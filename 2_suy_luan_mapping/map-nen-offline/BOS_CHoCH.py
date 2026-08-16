"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / map-nen-offline
- ROLE: Quét Swing High/Low, xác định cấu trúc vỡ trend SMC.
- I/O: IN: RAM (Nến) | OUT: RAM (Swing Points, Trend state)
- RULE: CHỈ tuân thủ ranh giới của khối, không cắm chéo.
"""

import time


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def _continuous_structure(
    trend, transition, swing_highs, swing_lows, last_close,
    last_close_time, broken_level=0.0, break_buffer=0.0, break_streak=0,
):
    """Đo độ mạnh từ nến đã đóng; không dùng bất kỳ giá sau close_time."""
    direction = 1.0 if trend == 'BULLISH' else -1.0 if trend == 'BEARISH' else 0.0
    if direction == 0.0:
        if 'BULLISH' in str(transition):
            direction = 1.0
        elif 'BEARISH' in str(transition):
            direction = -1.0

    span = max(
        abs(float((swing_highs or [last_close])[-1]) - float((swing_lows or [last_close])[-1])),
        abs(float(last_close or 0.0)) * 0.0002,
        1e-9,
    )
    progression = 0.0
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        if direction > 0.0:
            progression = (
                max(0.0, swing_highs[-1] - swing_highs[-2])
                + max(0.0, swing_lows[-1] - swing_lows[-2])
            ) / (2.0 * span)
        elif direction < 0.0:
            progression = (
                max(0.0, swing_highs[-2] - swing_highs[-1])
                + max(0.0, swing_lows[-2] - swing_lows[-1])
            ) / (2.0 * span)
    trend_strength = _clamp(progression * 4.0)
    if direction and trend_strength <= 0.0:
        trend_strength = min(0.55, 0.20 + 0.15 * int(break_streak or 0))
    break_strength = _clamp(
        abs(float(last_close or 0.0) - float(broken_level or 0.0))
        / max(abs(float(break_buffer or 0.0)), 0.10 * span, 1e-9)
    ) if broken_level else 0.0
    quality = _clamp(min(len(swing_highs), len(swing_lows)) / 4.0)
    return {
        'direction': direction,
        'trend_strength': trend_strength,
        'break_strength': break_strength,
        'quality': quality,
        'source_event_id': (
            f"m15:{int(last_close_time or 0)}:{trend}:{transition}"
        ),
    }

def get_macro_structure(klines_m15, pivot_legs=5, break_buffer=0.0, now_ms=None):
    """
    Tìm Swing High / Swing Low và xác định xu hướng (BOS / CHoCH).
    Khuyến nghị dùng với nến M15 (pivot_legs=5 → 75 phút mỗi bên).

    Args:
        klines_m15: list nến M15 (List of lists từ file json, [time, open, high, low, close...]).
        pivot_legs: số nến xét mỗi bên để xác nhận đỉnh/đáy (mặc định 5).

    Returns:
        dict {
            'trend'     : 'BULLISH' | 'BEARISH' | 'NEUTRAL',
            'swing_high': float  <- Swing High gần nhất,
            'swing_low' : float  <- Swing Low gần nhất,
            'all_sh'    : list   <- toàn bộ Swing High tìm được,
            'all_sl'    : list   <- toàn bộ Swing Low tìm được,
        }
    """
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)

    def is_closed(k):
        if isinstance(k, list) and len(k) > 6:
            return int(k[6]) <= now_ms
        if isinstance(k, dict):
            if 'x' in k:
                return bool(k['x'])
            close_time = k.get('T') or k.get('close_time')
            return not close_time or int(close_time) <= now_ms
        return True

    # Binance REST luôn kèm nến đang chạy; không cho close chưa hoàn tất đổi bias.
    klines_m15 = [k for k in klines_m15 if is_closed(k)]
    swing_highs = []
    swing_lows  = []

    # Hàm tiện ích lấy high/low an toàn từ raw list hoặc dict
    def get_h(k):
        return float(k[2]) if isinstance(k, list) else float(k['h'])
    def get_l(k):
        return float(k[3]) if isinstance(k, list) else float(k['l'])
    def get_c(k):
        return float(k[4]) if isinstance(k, list) else float(k['c'])
    def get_close_time(k):
        if isinstance(k, list):
            return int(k[6]) if len(k) > 6 else 0
        return int(k.get('T') or k.get('close_time') or k.get('t') or 0)

    # Bỏ qua pivot_legs nến đầu và cuối
    for i in range(pivot_legs, len(klines_m15) - pivot_legs):
        is_high = True
        is_low  = True

        curr_h = get_h(klines_m15[i])
        curr_l = get_l(klines_m15[i])

        for j in range(1, pivot_legs + 1):
            if curr_h <= get_h(klines_m15[i - j]) or curr_h <= get_h(klines_m15[i + j]):
                is_high = False
            if curr_l >= get_l(klines_m15[i - j]) or curr_l >= get_l(klines_m15[i + j]):
                is_low = False

        if is_high:
            swing_highs.append(curr_h)
        if is_low:
            swing_lows.append(curr_l)

    if not swing_highs or not swing_lows:
        last_close = get_c(klines_m15[-1]) if klines_m15 else 0.0
        last_close_time = get_close_time(klines_m15[-1]) if klines_m15 else 0
        return {
            "trend"     : "NEUTRAL",
            "swing_high": 0.0,
            "swing_low" : 0.0,
            "all_sh"    : swing_highs,
            "all_sl"    : swing_lows,
            "transition": "NONE",
            "last_close": last_close,
            "last_close_time": last_close_time,
            "break_streak": 0,
            "broken_level": 0.0,
            "continuous": _continuous_structure(
                'NEUTRAL', 'NONE', swing_highs, swing_lows,
                last_close, last_close_time,
            ),
        }

    last_sh = swing_highs[-1]
    last_sl = swing_lows[-1]
    trend   = "NEUTRAL"

    # BOS / CHoCH: Đỉnh+Đáy cao dần → BULLISH; thấp dần → BEARISH
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1]  > swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        ll = swing_lows[-1]  < swing_lows[-2]

        if hh and hl:
            trend = "BULLISH"   # Higher High + Higher Low → BOS tăng
        elif lh and ll:
            trend = "BEARISH"   # Lower High + Lower Low → BOS giảm

    transition = "NONE"
    break_streak = 0
    broken_level = 0.0
    last_close = get_c(klines_m15[-1])
    # Một cú xuyên rất nhỏ thường là sweep. Chỉ vô hiệu bias khi nến M15 đã
    # đóng vượt swing ít nhất max(buffer truyền vào, 2 bps giá).
    base_buffer = abs(float(break_buffer or 0.0))
    effective_buffer = max(base_buffer, last_close * 0.0002)
    structural_trend = trend
    if structural_trend == "BULLISH":
        broken_level = last_sl
        for candle in reversed(klines_m15):
            close = get_c(candle)
            candle_buffer = max(base_buffer, abs(close) * 0.0002)
            if close < broken_level - candle_buffer:
                break_streak += 1
            else:
                break
    elif structural_trend == "BEARISH":
        broken_level = last_sh
        for candle in reversed(klines_m15):
            close = get_c(candle)
            candle_buffer = max(base_buffer, abs(close) * 0.0002)
            if close > broken_level + candle_buffer:
                break_streak += 1
            else:
                break
    else:
        # NEUTRAL không có bias cũ để "invalidate", nhưng một range đã có
        # swing xác nhận vẫn có thể chuyển regime khi các nến M15 đóng hẳn
        # ngoài biên. Một close chỉ tạo candidate; close thứ hai mới xác nhận
        # transition để tránh biến sweep/wick đơn lẻ thành lệnh breakout.
        bullish_streak = 0
        bearish_streak = 0
        for candle in reversed(klines_m15):
            close = get_c(candle)
            candle_buffer = max(base_buffer, abs(close) * 0.0002)
            if close > last_sh + candle_buffer:
                bullish_streak += 1
            else:
                break
        for candle in reversed(klines_m15):
            close = get_c(candle)
            candle_buffer = max(base_buffer, abs(close) * 0.0002)
            if close < last_sl - candle_buffer:
                bearish_streak += 1
            else:
                break
        if bullish_streak:
            broken_level = last_sh
            break_streak = bullish_streak
            transition = (
                "NEUTRAL_BREAKOUT_BULLISH_CANDIDATE"
                if break_streak == 1 else "NEUTRAL_TRANSITION_BULLISH"
            )
        elif bearish_streak:
            broken_level = last_sl
            break_streak = bearish_streak
            transition = (
                "NEUTRAL_BREAKOUT_BEARISH_CANDIDATE"
                if break_streak == 1 else "NEUTRAL_TRANSITION_BEARISH"
            )

    if structural_trend == "BULLISH" and break_streak:
        trend = "NEUTRAL"
        transition = (
            "BULLISH_INVALIDATED"
            if break_streak == 1 else "TRANSITION_BEARISH"
        )
    elif structural_trend == "BEARISH" and break_streak:
        trend = "NEUTRAL"
        transition = (
            "BEARISH_INVALIDATED"
            if break_streak == 1 else "TRANSITION_BULLISH"
        )

    if not break_streak:
        broken_level = 0.0

    return {
        "trend"     : trend,
        "swing_high": last_sh,
        "swing_low" : last_sl,
        "all_sh"    : swing_highs,
        "all_sl"    : swing_lows,
        "transition": transition,
        "last_close": last_close,
        "last_close_time": get_close_time(klines_m15[-1]),
        "break_buffer": effective_buffer,
        "break_streak": break_streak,
        "broken_level": broken_level,
        "continuous": _continuous_structure(
            trend, transition, swing_highs, swing_lows, last_close,
            get_close_time(klines_m15[-1]), broken_level,
            effective_buffer, break_streak,
        ),
    }

