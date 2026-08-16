"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / tong_ket_chi_huy
- ROLE: Kiểm duyệt rủi ro (VETO GATE). Nếu cắm cờ Veto, lệnh sẽ bị từ chối ngay lập tức.
- I/O: Đọc state và mode_info -> Trả về (is_vetoed: bool, is_armed: bool, reason: str, bias: str)
"""

import time


FLASH_ADVERSE_SHARE_MIN = 0.65
FLASH_NET_P95_MULT = 1.50
FLASH_NET_THRESHOLD_RATIO = 0.35
WALL_CONFIRMATION_VERSION = 'WALL_PRICE_FLOW_V2'


def _flash_flow_evidence(state, armed_bias):
    buy = max(0.0, float(getattr(state, 'current_cvd_buy_3s', 0.0) or 0.0))
    sell = max(0.0, float(getattr(state, 'current_cvd_sell_3s', 0.0) or 0.0))
    p95 = max(0.0, float(getattr(state, 'p95_value', 5.0) or 0.0))
    vol_pct90 = max(0.0, float(getattr(state, 'vol_pct90', 0.0) or 0.0))
    threshold = max(p95 * 3.0, vol_pct90)
    adverse = sell if armed_bias == 'LONG' else buy
    supporting = buy if armed_bias == 'LONG' else sell
    total = adverse + supporting
    adverse_share = adverse / total if total > 0.0 else 0.0
    net_adverse = adverse - supporting
    net_floor = max(
        p95 * FLASH_NET_P95_MULT,
        threshold * FLASH_NET_THRESHOLD_RATIO,
    )
    confirmed = bool(
        adverse > threshold
        and adverse_share >= FLASH_ADVERSE_SHARE_MIN
        and net_adverse >= net_floor
    )
    return {
        'confirmed': confirmed,
        'adverse_qty': adverse,
        'supporting_qty': supporting,
        'adverse_share': adverse_share,
        'net_adverse_qty': net_adverse,
        'threshold_qty': threshold,
        'net_floor_qty': net_floor,
    }


def remember_confirmed_flash(target_state, source, armed_bias, now=None):
    """Persist a bounded severity trace after a confirmed hard Flash veto.

    The trace is only an input for continuous scoring/toxic-fill handling.  It
    does not extend the hard VETO window and therefore cannot become a hidden
    Boolean cooldown.
    """
    evidence = _flash_flow_evidence(source, armed_bias)
    if not evidence['confirmed'] or armed_bias not in ('LONG', 'SHORT'):
        return None
    now = float(now if now is not None else time.time())
    share_excess = max(0.0, min(
        1.0,
        (evidence['adverse_share'] - FLASH_ADVERSE_SHARE_MIN)
        / max(1.0 - FLASH_ADVERSE_SHARE_MIN, 1e-9),
    ))
    net_ratio = evidence['net_adverse_qty'] / max(
        evidence['net_floor_qty'], 1e-9
    )
    net_excess = max(0.0, min(1.0, (net_ratio - 1.0) / 1.5))
    threshold_ratio = evidence['adverse_qty'] / max(
        evidence['threshold_qty'], 1e-9
    )
    magnitude_excess = max(0.0, min(1.0, (threshold_ratio - 1.0) / 1.5))
    severity = max(0.0, min(
        1.0, 0.35 + 0.30 * share_excess
        + 0.25 * net_excess + 0.10 * magnitude_excess,
    ))
    registry = getattr(target_state, 'adverse_flow_memory_by_bias', None)
    if not isinstance(registry, dict):
        registry = {'LONG': {}, 'SHORT': {}}
        target_state.adverse_flow_memory_by_bias = registry
    previous = dict(registry.get(armed_bias) or {})
    record = {
        'ts': now,
        'blocked_bias': armed_bias,
        'severity': severity,
        'adverse_share': evidence['adverse_share'],
        'net_adverse_qty': evidence['net_adverse_qty'],
        'net_floor_qty': evidence['net_floor_qty'],
        'adverse_qty': evidence['adverse_qty'],
        'threshold_qty': evidence['threshold_qty'],
        'source_event_id': (
            previous.get('source_event_id')
            if now - float(previous.get('ts', 0.0) or 0.0) <= 3.5
            else f'flash:{armed_bias}:{int(now * 1000)}'
        ),
        'contract_version': 'FLASH_ADVERSE_MEMORY_V1',
    }
    registry[armed_bias] = record
    return record


def _confirmed_wall_pull(wall_pull):
    """Không tin một boolean/OBI đơn lẻ; bắt buộc contract price + aggTrade."""
    return bool(
        wall_pull.get('confirmation_version') == WALL_CONFIRMATION_VERSION
        and wall_pull.get('confirmed_for_veto') is True
        and wall_pull.get('price_confirmed') is True
        and wall_pull.get('flow_corroborated') is True
    )


def kiem_tra_veto(state, armed_bias):
    """
    1. Kiểm tra các điều kiện VETO.
    Trả về: (is_vetoed, reason)
    """
    if armed_bias not in ('LONG', 'SHORT'):
        return True, "Thiếu Bias (Chưa được cấp phép bắn)"
        
    # [TIER S - TRI-ORACLE] VETO tuyet doi khi phat hien phan ky cheo san
    if getattr(state, 'tri_oracle_signal', 'NEUTRAL') == 'DIVERGENCE':
        return True, "VETO — Cá mập đang dùng Futures dọa, Coinbase nói ngược lại"
        
    # --- ĐÃ FULL-ARM, CHỈ BẮT ĐẦU VETO ---
    
    # 2. VETO Flash Flow (Xả/Bơm ngược chiều quá mạnh trong 3s)
    flash = _flash_flow_evidence(state, armed_bias)
    
    best_bid = getattr(state, 'best_bid', 0.0)
    best_ask = getattr(state, 'best_ask', 0.0)
    
    # Một phía vừa nhỉnh hơn 50% rất dễ là nhiễu của tumbling-window. Hard VETO
    # chỉ dành cho burst vừa lớn, vừa chiếm >=65%, vừa có net delta đủ vật chất.
    if flash['confirmed']:
        action = 'XẢ ngược hướng LONG' if armed_bias == 'LONG' else 'BƠM ngược hướng SHORT'
        return True, (
            f"VETO: Flash Flow {action} ({flash['adverse_qty']:.2f} BTC; "
            f"share={flash['adverse_share']:.0%}; net={flash['net_adverse_qty']:.2f})"
        )
        
    # 3. VETO Wall Pull: disappearance/OBI cùng thuộc một nguồn depth nên chỉ
    # advisory. Chỉ price follow-through + aggTrade corroboration mới được veto.
    wall_pull = getattr(state, 'wall_pull_flag', {'active': False}) or {}
    if wall_pull.get('active') and time.time() - wall_pull.get('ts', 0.0) <= 1.0:
        dangerous = (
            (armed_bias == 'LONG' and wall_pull.get('side') == 'buy')
            or (armed_bias == 'SHORT' and wall_pull.get('side') == 'sell')
        )
        if dangerous and _confirmed_wall_pull(wall_pull):
            return True, (
                "VETO: Wall Pull đã xác nhận bằng giá+flow "
                f"(move={float(wall_pull.get('price_displacement_bps', 0.0)):.2f}bps; "
                f"flow={float(wall_pull.get('adverse_flow_share', 0.0)):.0%})"
            )
        
    # 4. VETO Spread dãn
    if best_bid > 0:
        spread_pct = (best_ask - best_bid) / best_bid * 100
        if spread_pct > 0.1:
            return True, f"VETO: Spread quá lớn ({spread_pct:.3f}%)"
        
    return False, "PASS"
