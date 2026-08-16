"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping
- ROLE: Tính toán EMA9 của M1 đang chạy.
- I/O: IN: RAM (Nến live 1m) | OUT: RAM (ema9_m1)
"""

import asyncio
import logging
import time


BREAKOUT_WINDOW = 3
BREAKOUT_SINGLE_BODY_ATR = 1.0
BREAKOUT_CUMULATIVE_ATR = 1.0
BREAKOUT_MIN_EXTENSION_ATR = 0.10
BREAKOUT_MIN_CLOSES_BEYOND = 2
BREAKOUT_MIN_DIRECTIONAL_CANDLES = 2
BREAKOUT_MIN_BODY_RANGE_RATIO = 0.50


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def measure_sweep_m1(candle, liquidity_level, direction_bias, atr=0.0):
    """Return contract Boolean cũ kèm phép đo causal từ đúng nến đã đóng."""
    range_ = float(candle.get('h', 0.0)) - float(candle.get('l', 0.0))
    atr = float(atr or 0.0)
    if range_ <= 0.0:
        return False, None, {
            'active': False, 'direction': 0.0, 'strength': 0.0,
            'quality': 0.0, 'quality_flags': ['INVALID_CANDLE_RANGE'],
        }

    candidates = []
    for side, level_name, sign in (
        ('LONG', 'ssl', 1.0), ('SHORT', 'bsl', -1.0),
    ):
        level = float(liquidity_level.get(level_name, 0.0) or 0.0)
        if level <= 0.0 or direction_bias not in (side, 'NEUTRAL'):
            continue
        if side == 'LONG':
            penetration = max(0.0, level - float(candle['l']))
            reclaim = float(candle['c']) - level
            wick = min(float(candle['o']), float(candle['c'])) - float(candle['l'])
        else:
            penetration = max(0.0, float(candle['h']) - level)
            reclaim = level - float(candle['c'])
            wick = float(candle['h']) - max(float(candle['o']), float(candle['c']))
        crossed = penetration > 0.0
        closed_back = reclaim > 0.0
        wick_ratio = _clamp(wick / range_)
        penetration_atr = penetration / atr if atr > 0.0 else 0.0
        reclaim_atr = max(0.0, reclaim) / atr if atr > 0.0 else 0.0
        penetration_materiality = (
            _clamp(penetration_atr / 0.05)
            * _clamp((1.50 - penetration_atr) / 1.10)
            if atr > 0.0 else float(crossed)
        )
        zone_precision = _clamp(1.0 - max(0.0, penetration_atr - 0.35) / 1.15)
        reclaim_quality = _clamp(reclaim_atr / 0.10)
        strength = (
            penetration_materiality * (0.45 * wick_ratio + 0.55 * reclaim_quality)
            if crossed else 0.0
        )
        quality = _clamp(
            0.45 * wick_ratio + 0.35 * zone_precision + 0.20 * reclaim_quality
        )
        qualified = bool(crossed and closed_back and wick_ratio >= 0.50)
        candidates.append({
            'qualified': qualified,
            'side': side,
            'direction': sign,
            'active': crossed,
            'strength': _clamp(strength),
            'quality': quality,
            'penetration_atr': penetration_atr,
            'wick_ratio': wick_ratio,
            'reclaim_distance_atr': reclaim_atr,
            # M1 OHLC không chứa đường đi intrabar; không được bịa tốc độ.
            'reclaim_speed': 0.0,
            'zone_precision': zone_precision,
            'volume_materiality': 0.0,
            'mae_atr': penetration_atr,
            'level': level,
            'quality_flags': [
                'RECLAIM_SPEED_UNAVAILABLE', 'VOLUME_BASELINE_UNAVAILABLE',
            ],
        })
    if not candidates:
        return False, None, {
            'active': False, 'direction': 0.0, 'strength': 0.0,
            'quality': 0.0, 'quality_flags': ['NO_LIQUIDITY_LEVEL'],
        }
    # Boolean live giữ đúng precedence cũ LONG rồi SHORT. Continuous chỉ dùng
    # candidate mạnh nhất khi không có sweep nào đủ điều kiện.
    qualified = [item for item in candidates if item['qualified']]
    selected = qualified[0] if qualified else max(
        candidates, key=lambda item: (item['strength'], item['quality'])
    )
    return bool(qualified), selected['side'] if qualified else None, selected

def check_sweep_m1(candle, liquidity_level, direction_bias):
    """
    Kiểm tra xem nến M1 có quét thanh khoản (Sweep) thành công hay không.
    """
    flag, direction, _ = measure_sweep_m1(
        candle, liquidity_level, direction_bias
    )
    return flag, direction

def detect_breakout_m1(candles, liquidity_level, atr):
    """Nhận diện breakout hai chiều, độc lập với bias M15 hiện tại.

    Giữ đường xác nhận một nến mạnh, đồng thời nhận displacement tích lũy
    trong tối đa ba nến. Cumulative path bắt buộc hai close ngoài level và
    hai nến cùng hướng để không biến một wick đơn lẻ thành breakout.
    """
    atr = float(atr or 0.0)
    clean = [
        {
            't': int(candle.get('t', 0) or 0),
            'o': float(candle.get('o', 0.0) or 0.0),
            'h': float(candle.get('h', 0.0) or 0.0),
            'l': float(candle.get('l', 0.0) or 0.0),
            'c': float(candle.get('c', 0.0) or 0.0),
        }
        for candle in list(candles or ())[-BREAKOUT_WINDOW:]
    ]
    clean = [
        candle for candle in clean
        if min(candle['o'], candle['h'], candle['l'], candle['c']) > 0.0
        and candle['h'] >= candle['l']
    ]
    if atr <= 0.0 or not clean:
        return False, None, {}

    last = clean[-1]
    last_range = last['h'] - last['l']
    last_body = abs(last['c'] - last['o'])
    strong_single = bool(
        last_range > 0.0
        and last_body / last_range > BREAKOUT_MIN_BODY_RANGE_RATIO
        and last_body >= BREAKOUT_SINGLE_BODY_ATR * atr
    )

    def evaluate(direction, level):
        level = float(level or 0.0)
        if level <= 0.0:
            return None
        sign = 1.0 if direction == 'LONG' else -1.0
        crossed_single = bool(
            sign * (last['o'] - level) <= 0.0
            and sign * (last['c'] - level) > 0.0
            and sign * (last['c'] - last['o']) > 0.0
        )
        if strong_single and crossed_single:
            return {
                'detection': 'SINGLE_DISPLACEMENT',
                'level': level,
                'window': 1,
                'displacement_atr': round(last_body / atr, 4),
                'extension_atr': round(sign * (last['c'] - level) / atr, 4),
                'closes_beyond': 1,
            }

        if len(clean) < 2:
            return None
        prior_points = [clean[0]['o']] + [item['c'] for item in clean[:-1]]
        approached_from_inside = any(
            sign * (point - level) <= 0.0 for point in prior_points
        )
        closes_beyond = sum(
            sign * (item['c'] - level) > 0.0 for item in clean
        )
        directional = sum(
            sign * (item['c'] - item['o']) > 0.0 for item in clean
        )
        displacement = sign * (clean[-1]['c'] - clean[0]['o'])
        extension = sign * (clean[-1]['c'] - level)
        if (
            approached_from_inside
            and closes_beyond >= BREAKOUT_MIN_CLOSES_BEYOND
            and directional >= BREAKOUT_MIN_DIRECTIONAL_CANDLES
            and displacement >= BREAKOUT_CUMULATIVE_ATR * atr
            and extension >= BREAKOUT_MIN_EXTENSION_ATR * atr
        ):
            return {
                'detection': 'CUMULATIVE_DISPLACEMENT',
                'level': level,
                'window': len(clean),
                'displacement_atr': round(displacement / atr, 4),
                'extension_atr': round(extension / atr, 4),
                'closes_beyond': closes_beyond,
            }
        return None

    long_meta = evaluate('LONG', liquidity_level.get('bsl', 0.0))
    short_meta = evaluate('SHORT', liquidity_level.get('ssl', 0.0))
    if long_meta:
        return True, 'LONG', long_meta
    if short_meta:
        return True, 'SHORT', short_meta
    return False, None, {}


def check_breakout_m1(candle, liquidity_level, direction_bias, atr):
    """Compatibility helper cho unit cũ; runtime dùng detector hai chiều."""
    flag, direction, _ = detect_breakout_m1([candle], liquidity_level, atr)
    allowed = direction_bias in (direction, 'NEUTRAL')
    return (flag and allowed), (direction if flag and allowed else None)

def cap_nhat_nen_m1(nen_1m: dict, state):
    """
    Tính EMA9 nhanh trên nến M1 hiện tại và check Sweep khi nến đóng.
    """
    current_price = nen_1m.get('c', 0.0)
    if current_price == 0.0:
        return
        
    # 1. Tính toán EMA9 (chỉ tính khi nến đóng để tránh làm mượt quá mức bởi tick)
    if nen_1m.get('x') == True:
        open_time = int(nen_1m.get('t', 0) or 0)
        if open_time and open_time == getattr(state, 'last_processed_m1_live', 0):
            return
        state.last_processed_m1_live = open_time
        if getattr(state, 'ema9_m1', 0.0) == 0.0:
            state.ema9_m1 = current_price
        else:
            state.ema9_m1 = (current_price - state.ema9_m1) * 0.2 + state.ema9_m1

    # 2. Check Sweep Râu khi nến M1 báo đóng (x=True)
    if nen_1m.get('x') == True:
        liquidity_level = {
            'ssl': getattr(state, 'swing_low_m15', 0.0),
            'bsl': getattr(state, 'swing_high_m15', 0.0)
        }
        current_mode = getattr(state, 'current_mode', {})
        direction_bias = current_mode.get('bias', 'NEUTRAL')
        if current_mode.get('m15_scoring_only'):
            direction_bias = 'NEUTRAL'
        
        atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
        flag, direction, sweep_meta = measure_sweep_m1(
            nen_1m, liquidity_level, direction_bias, atr
        )
        
        if flag:
            logging.info(f"🧹 [SWEEP M1] Đã quét thanh khoản thành công. Hướng: {direction}")
            
        ts = time.time()
            
        state.sweep_m1 = {
            'flag': flag,
            'direction': direction,
            'ts': ts,
            'level': nen_1m['l'] if direction == 'LONG' else (nen_1m['h'] if direction == 'SHORT' else 0.0),
            'event_id': f"sweep:{open_time}:{direction}" if flag else None,
        }

        continuous_sweep = {
            **sweep_meta,
            'ts': ts,
            'ttl': 120.0,
            'source_event_id': (
                f"sweep-candidate:{open_time}:{sweep_meta.get('side')}"
            ),
            'event_id': f"sweep:{open_time}:{direction}" if flag else None,
            'source_family': 'PRICE_REACTION',
        }
        if continuous_sweep != getattr(state, 'continuous_sweep_m1', {}):
            state.continuous_sweep_m1 = continuous_sweep
            state.continuous_evidence_revision = int(
                getattr(state, 'continuous_evidence_revision', 0)
            ) + 1

        # 3. Breakout phải được detect hai chiều, kể cả khi ngược bias M15 cũ.
        history = getattr(state, 'breakout_m1_history', None)
        if history is None:
            history = []
            state.breakout_m1_history = history
        history.append({
            key: nen_1m.get(key) for key in ('t', 'o', 'h', 'l', 'c')
        })
        if not hasattr(history, 'maxlen') and len(history) > BREAKOUT_WINDOW:
            del history[:-BREAKOUT_WINDOW]
        brk_flag, brk_direction, brk_meta = detect_breakout_m1(
            history, liquidity_level, atr
        )
        if brk_flag:
            logging.info(
                "🚀 [BREAKOUT M1] %s %s level=%.2f displacement=%.2f ATR",
                brk_meta.get('detection'), brk_direction,
                brk_meta.get('level', 0.0),
                brk_meta.get('displacement_atr', 0.0),
            )
            
        state.breakout_m1 = {
            'flag': brk_flag,
            'direction': brk_direction,
            'ts': ts,
            **brk_meta,
            'event_id': f"breakout:{open_time}:{brk_direction}" if brk_flag else None,
        }
        displacement = float(brk_meta.get('displacement_atr', 0.0) or 0.0)
        extension = max(
            0.0, float(brk_meta.get('extension_atr', 0.0) or 0.0)
        )
        closes = int(brk_meta.get('closes_beyond', 0) or 0)
        continuous_breakout = {
            'active': bool(brk_flag),
            'direction': (
                1.0 if brk_direction == 'LONG'
                else -1.0 if brk_direction == 'SHORT' else 0.0
            ),
            'strength': _clamp(
                0.55 * displacement + 0.25 * extension + 0.10 * closes
            ),
            'quality': (
                _clamp(0.45 + 0.20 * closes + 0.20 * min(displacement, 1.0))
                if brk_flag else 0.0
            ),
            'freshness': 1.0,
            'ts': ts,
            'ttl': 15.0,
            'source_event_id': (
                f"breakout:{open_time}:{brk_direction}" if brk_flag else None
            ),
            'event_id': (
                f"breakout:{open_time}:{brk_direction}" if brk_flag else None
            ),
            'source_family': 'M1_CLOSED',
            'dependency_families': ['M1_CLOSED', 'PRICE_REACTION'],
            **brk_meta,
        }
        if continuous_breakout != getattr(state, 'continuous_breakout_m1', {}):
            state.continuous_breakout_m1 = continuous_breakout
            state.continuous_evidence_revision = int(
                getattr(state, 'continuous_evidence_revision', 0)
            ) + 1
        state.decision_revision = getattr(state, 'decision_revision', 0) + 1
