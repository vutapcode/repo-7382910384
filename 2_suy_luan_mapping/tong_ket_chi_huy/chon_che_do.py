"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / tong_ket_chi_huy
- ROLE: Chọn bộ luật giao dịch dựa trên bối cảnh thị trường.
- I/O: Đọc state (Trend, POC, VAH, VAL, ATR) -> Trả về dictionary (Modes, Bias, Zones).
"""

import os

from loi_he_thong import strategy_profile


NEUTRAL_STRUCTURAL_EXECUTION_ENABLED = os.getenv(
    'SMC_NEUTRAL_STRUCTURAL_EXECUTION_ENABLED', 'false'
).lower() in ('1', 'true', 'yes', 'on')

def xac_dinh_che_do(state):
    """
    Đọc bối cảnh và đưa ra chiến lược (Mode) hợp lý.
    """
    trend = getattr(state, 'trend_m15', 'NEUTRAL')
    poc = getattr(state, 'poc', 0.0)
    vah = getattr(state, 'vah', 0.0)
    val = getattr(state, 'val', 0.0)
    atr = getattr(state, 'atr_1m', 0.0)
    swing_high = getattr(state, 'swing_high_m15', 0.0)
    swing_low = getattr(state, 'swing_low_m15', 0.0)
    structure_transition = getattr(state, 'structure_transition', 'NONE')
    broken_level = getattr(state, 'structure_broken_level', 0.0)

    # Chưa có đủ dữ liệu -> STANDBY
    if poc == 0.0 or atr == 0.0:
        return {'modes': ['STANDBY'], 'bias': 'NONE'}

    # Nến phá đầu tiên chỉ tạo candidate/đứng ngoài. Từ nến đóng xác nhận thứ
    # hai, structural breakout đi qua lifecycle WAIT_RETEST; không market-entry
    # trực tiếp và fee gate vẫn là lớp bắt buộc ở Executor.
    if structure_transition == 'NEUTRAL_TRANSITION_BEARISH':
        zones = list(dict.fromkeys(
            float(level) for level in (broken_level, poc, vah) if level > 0.0
        ))
        return {
            'modes': ['TRANSITION-PULLBACK', 'TRANSITION-BREAKOUT'],
            'bias': 'SHORT',
            'pullback_zones': zones,
            'breakout_level': float(broken_level),
            'breakout_event_id': (
                f"m15:neutral-break:SHORT:{float(broken_level):.8f}"
            ),
            'reason': structure_transition,
            'size_cap_pct': 50,
            'advisory_only': not NEUTRAL_STRUCTURAL_EXECUTION_ENABLED,
        }
    if structure_transition == 'NEUTRAL_TRANSITION_BULLISH':
        zones = list(dict.fromkeys(
            float(level) for level in (broken_level, poc, val) if level > 0.0
        ))
        return {
            'modes': ['TRANSITION-PULLBACK', 'TRANSITION-BREAKOUT'],
            'bias': 'LONG',
            'pullback_zones': zones,
            'breakout_level': float(broken_level),
            'breakout_event_id': (
                f"m15:neutral-break:LONG:{float(broken_level):.8f}"
            ),
            'reason': structure_transition,
            'size_cap_pct': 50,
            'advisory_only': not NEUTRAL_STRUCTURAL_EXECUTION_ENABLED,
        }

    # Đảo chiều từ một trend đã tồn tại giữ contract pullback cũ. Nó khác với
    # breakout thoát range NEUTRAL ở trên và không được nhập chung lifecycle.
    if structure_transition == 'TRANSITION_BEARISH':
        zones = list(dict.fromkeys(
            float(level) for level in (broken_level, poc, vah) if level > 0.0
        ))
        return {
            'modes': ['TRANSITION-PULLBACK'],
            'bias': 'SHORT',
            'pullback_zones': zones,
            'reason': structure_transition,
            'size_cap_pct': 50,
        }
    if structure_transition == 'TRANSITION_BULLISH':
        zones = list(dict.fromkeys(
            float(level) for level in (broken_level, poc, val) if level > 0.0
        ))
        return {
            'modes': ['TRANSITION-PULLBACK'],
            'bias': 'LONG',
            'pullback_zones': zones,
            'reason': structure_transition,
            'size_cap_pct': 50,
        }

    # Không đảo SHORT/LONG ngay ở cây nến vừa vô hiệu cấu trúc cũ.
    if structure_transition != 'NONE':
        return {
            'modes': ['STANDBY'],
            'bias': 'NONE',
            'reason': structure_transition,
        }

    if trend == 'BULLISH':
        result = {
            'modes': ['TREND-PULLBACK', 'TREND-BREAKOUT'],
            'bias': 'LONG',
            'pullback_zones': [poc, val],
            'breakout_level': swing_high,
            # M15 là context chấm +/-0.5, không còn độc quyền hướng candidate.
            # Giữ các field legacy phía trên cho context detector hiện hữu;
            # Radar đọc lane hai chiều và chỉ claim khi M1/flow đủ CORE.
            'm15_scoring_only': True,
            'candidate_lanes': [
                {'bias': 'LONG', 'pullback_zones': [poc, val],
                 'breakout_level': swing_high},
                {'bias': 'SHORT', 'pullback_zones': [poc, vah],
                 'breakout_level': swing_low},
            ],
        }
        if strategy_profile.adaptive_overfit_enabled() and vah > 0.0:
            # A value boundary that has migrated with the trend is a retest
            # location, not an independent LONG vote. CONTINUOUS_V2 must still
            # confirm causal acceptance/momentum and the order stays passive.
            result['adaptive_retest_zones'] = [{
                'bias': 'LONG', 'zone': float(vah), 'boundary': 'VAH',
                'role': 'VALUE_MIGRATION_RETEST',
            }]
        return result
        
    elif trend == 'BEARISH':
        result = {
            'modes': ['TREND-PULLBACK', 'TREND-BREAKOUT'],
            'bias': 'SHORT',
            'pullback_zones': [poc, vah],
            'breakout_level': swing_low,
            'm15_scoring_only': True,
            'candidate_lanes': [
                {'bias': 'SHORT', 'pullback_zones': [poc, vah],
                 'breakout_level': swing_low},
                {'bias': 'LONG', 'pullback_zones': [poc, val],
                 'breakout_level': swing_high},
            ],
        }
        if strategy_profile.adaptive_overfit_enabled() and val > 0.0:
            result['adaptive_retest_zones'] = [{
                'bias': 'SHORT', 'zone': float(val), 'boundary': 'VAL',
                'role': 'VALUE_MIGRATION_RETEST',
            }]
        return result
        
    else: # NEUTRAL
        range_size = vah - val
        # Chỉ đánh Sideway (Fade) nếu biên độ đủ rộng (>= 2 ATR) để bù spread
        if range_size >= 2 * atr:
            return {
                'modes': ['NEUTRAL-FADE'],
                'bias': 'NEUTRAL', # Sideway có thể đánh 2 chiều (Chạm VAH short, VAL long)
                'zone_long': val,
                'zone_short': vah
            }
        else:
            return {
                'modes': ['STANDBY'], # Biên độ hẹp -> Ngồi ngoài
                'bias': 'NONE'
            }
