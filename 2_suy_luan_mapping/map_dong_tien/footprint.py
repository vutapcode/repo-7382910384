"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / map_dong_tien
- ROLE: Xây dựng Footprint Chart từ dòng tiền, phát hiện Stacked Imbalance.
- I/O: IN: RAM (Lệnh khớp) | OUT: RAM (Stacked Imbalance State)
- RULE: CHỈ tuân thủ ranh giới của khối, không cắm chéo.
"""

from collections import defaultdict
import logging
import math
import time

CANDLE_INTERVAL_MS = 60_000
TICK_SIZE = 5.0
IMBALANCE_RATIO = 3.0
STACK_MIN = 3
MIN_DIAGONAL_VOLUME = 0.01
LIVE_EVAL_INTERVAL = 0.10
ACTIVE_ARM_STATES = {'PRE_ARM', 'WATCH', 'ARMED_WINDOW', 'EXECUTING'}

def price_to_tick(price: float) -> int:
    return round(price / TICK_SIZE)

def _tinh_stacked_imbalances(rows: dict) -> list:
    """
    So sánh diagonal footprint đối xứng:
    - Buy(tick)  so với Sell(tick-1).
    - Sell(tick) so với Buy(tick+1).

    Bỏ mẫu có mẫu số quá nhỏ để một lệnh bụi không tạo ratio giả.
    """
    if not rows:
        return []
        
    sorted_ticks = sorted(rows.keys())
    n = len(sorted_ticks)
    flags = [None] * n

    for i, tick in enumerate(sorted_ticks):
        buy_vol, sell_vol = rows[tick]
        lower_sell = rows.get(tick - 1, [0.0, 0.0])[1]
        upper_buy = rows.get(tick + 1, [0.0, 0.0])[0]

        if (
            lower_sell >= MIN_DIAGONAL_VOLUME
            and buy_vol >= lower_sell * IMBALANCE_RATIO
        ):
            flags[i] = "buy"
        elif (
            upper_buy >= MIN_DIAGONAL_VOLUME
            and sell_vol >= upper_buy * IMBALANCE_RATIO
        ):
            flags[i] = "sell"

    stacks = []
    i = 0
    while i < n:
        if flags[i] is None:
            i += 1
            continue
        j = i
        while (
            j + 1 < n
            and flags[j + 1] == flags[i]
            and sorted_ticks[j + 1] == sorted_ticks[j] + 1
        ):
            j += 1
        if j - i + 1 >= STACK_MIN:
            run_ticks = sorted_ticks[i:j + 1]
            if flags[i] == 'buy':
                dominant_volume = sum(rows[tick][0] for tick in run_ticks)
                opposite_volume = sum(
                    rows.get(tick - 1, [0.0, 0.0])[1]
                    for tick in run_ticks
                )
            else:
                dominant_volume = sum(rows[tick][1] for tick in run_ticks)
                opposite_volume = sum(
                    rows.get(tick + 1, [0.0, 0.0])[0]
                    for tick in run_ticks
                )
            total_volume = sum(
                rows[tick][0] + rows[tick][1] for tick in run_ticks
            )
            stacks.append({
                "direction" : flags[i],
                "price_low" : sorted_ticks[i] * TICK_SIZE,
                "price_high": sorted_ticks[j] * TICK_SIZE,
                "count"     : j - i + 1,
                "dominant_volume": dominant_volume,
                "opposite_volume": opposite_volume,
                "total_volume": total_volume,
                "imbalance_ratio": (
                    dominant_volume / max(opposite_volume, MIN_DIAGONAL_VOLUME)
                ),
                "materiality": min(
                    1.0,
                    1.0 - math.exp(
                        -dominant_volume
                        / max(1.0, float(j - i + 1))
                    ),
                ),
                "price_span": (sorted_ticks[j] - sorted_ticks[i] + 1) * TICK_SIZE,
            })
        i = j + 1
    return stacks


def _publish_strongest_stack(state, stacks, reference_price, now, realtime):
    """Phát đúng một event mạnh nhất/gần giá nhất, không spam cùng event."""
    if not stacks:
        return False
    strongest = max(
        stacks,
        key=lambda item: (
            item['count'],
            -abs((item['price_low'] + item['price_high']) / 2.0 - reference_price),
        ),
    )
    event_id = (
        f"fp:{state.fp_current_candle['open_time']}:"
        f"{strongest['direction']}:{strongest['price_low']}:{strongest['price_high']}"
    )
    previous = getattr(state, 'fp_last_imbalance', {}) or {}
    count_strength = min(1.0, float(strongest['count']) / 6.0)
    ratio_strength = min(1.0, float(strongest['imbalance_ratio']) / 8.0)
    materiality = float(strongest['materiality'])
    continuous = {
        'active': True,
        'direction': 1.0 if strongest['direction'] == 'buy' else -1.0,
        'strength': min(
            1.0, 0.35 * count_strength + 0.35 * ratio_strength + 0.30 * materiality
        ),
        'quality': min(1.0, 0.45 * ratio_strength + 0.35 * materiality + 0.20 * count_strength),
        'ts': now,
        'ttl': 15.0,
        'source_event_id': event_id,
        'event_id': event_id,
        'source_family': 'AGGTRADE',
        'dependency_families': ['AGGTRADE', 'FOOTPRINT_CANDLE'],
        'dominant_volume': strongest['dominant_volume'],
        'opposite_volume': strongest['opposite_volume'],
        'total_volume': strongest['total_volume'],
        'imbalance_ratio': strongest['imbalance_ratio'],
        'materiality': materiality,
        'price_low': strongest['price_low'],
        'price_high': strongest['price_high'],
        'price_span': strongest['price_span'],
        'count': strongest['count'],
        'realtime': bool(realtime),
    }
    old_continuous = getattr(state, 'continuous_footprint', {}) or {}
    continuous_changed = (
        old_continuous.get('source_event_id') != event_id
        or abs(float(old_continuous.get('strength', 0.0)) - continuous['strength']) >= 0.01
        or abs(float(old_continuous.get('quality', 0.0)) - continuous['quality']) >= 0.01
        or abs(
            float(old_continuous.get('dominant_volume', 0.0))
            - float(continuous['dominant_volume'])
        ) >= max(0.01, 0.05 * float(old_continuous.get('dominant_volume', 0.0)))
    )
    state.continuous_footprint = continuous
    if continuous_changed:
        state.continuous_evidence_revision = int(
            getattr(state, 'continuous_evidence_revision', 0)
        ) + 1
    if previous.get('event_id') == event_id:
        return False
    state.fp_last_imbalance = {
        'dir': strongest['direction'],
        'ts': now,
        'used': False,
        'event_id': event_id,
        'price_low': strongest['price_low'],
        'price_high': strongest['price_high'],
        'count': strongest['count'],
        'realtime': bool(realtime),
    }
    state.decision_revision = getattr(state, 'decision_revision', 0) + 1
    return True


def _evaluate_live_near_zone(state, reference_price):
    """Chỉ chạy phép quét footprint tốn hơn khi Radar đã tới gần vùng."""
    if getattr(state, 'arm_state', 'IDLE') not in ACTIVE_ARM_STATES:
        return
    now_mono = time.monotonic()
    last_eval = float(getattr(state, 'fp_last_eval_mono', 0.0) or 0.0)
    if now_mono - last_eval < LIVE_EVAL_INTERVAL:
        return
    state.fp_last_eval_mono = now_mono
    stacks = _tinh_stacked_imbalances(state.fp_current_candle['rows'])
    _publish_strongest_stack(
        state, stacks, reference_price, time.time(), realtime=True
    )

def cap_nhat_footprint(lenh_khop: dict, state):
    """
    Hàm Pure Function cập nhật Footprint.
    """
    ts = lenh_khop.get('thoi_gian_ms', lenh_khop.get('thoi_gian', 0))
    if ts == 0:
        return
        
    price = lenh_khop['gia']
    qty = lenh_khop['khoi_luong']
    is_buyer_maker = lenh_khop['ban_chu_dong']
    
    candle_open = (int(ts) // CANDLE_INTERVAL_MS) * CANDLE_INTERVAL_MS
    
    # 1. Quản lý vòng đời nến
    if state.fp_current_candle is None:
        state.fp_current_candle = {'open_time': candle_open, 'rows': defaultdict(lambda: [0.0, 0.0])}
    elif candle_open != state.fp_current_candle['open_time']:
        # Đóng nến cũ
        stacks = _tinh_stacked_imbalances(state.fp_current_candle['rows'])
        _publish_strongest_stack(
            state, stacks, price, time.time(), realtime=False
        )
            
        state.fp_candles.append(state.fp_current_candle)
        # Tạo nến mới
        state.fp_current_candle = {'open_time': candle_open, 'rows': defaultdict(lambda: [0.0, 0.0])}
        
    # 2. Thêm trade vào nến hiện tại
    tick = price_to_tick(price)
    if is_buyer_maker:
        state.fp_current_candle['rows'][tick][1] += qty  # sell
    else:
        state.fp_current_candle['rows'][tick][0] += qty  # buy

    _evaluate_live_near_zone(state, price)
