"""Context continuation: flow 15s/60s và rejection POC/VAH/VAL có xác nhận."""

import time


UPDATE_INTERVAL = 0.20
PRICE_SAMPLE_INTERVAL = 0.20
FLOW_FAST_SECONDS = 15
FLOW_SLOW_SECONDS = 60
FLOW_FAST_IMBALANCE_MIN = 0.30
FLOW_SLOW_IMBALANCE_MIN = 0.15
FLOW_BUCKET_IMBALANCE_MIN = 0.05
FLOW_PRICE_CONFIRM_ATR = 0.05
FLOW_PRICE_TRAP_ATR = 0.05
FLOW_EVENT_TTL = 5.0

ZONE_TOUCH_ATR = 0.12
ZONE_REJECTION_ATR = 0.18
ZONE_ACCEPTANCE_ATR = 0.10
ZONE_INVALIDATION_ATR = 0.35
ZONE_MIN_AGE = 0.80
ZONE_ACCEPTANCE_SECONDS = 2.0
ZONE_MAX_AGE = 20.0
ZONE_EVENT_TTL = 15.0
TRAP_EVENT_TTL = 8.0
ZONE_RECLAIM_ATR = 0.05


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def _continuous_fingerprint(event):
    values = []
    for key in sorted(event):
        if key in ('ts', 'observed_at'):
            continue
        value = event[key]
        if isinstance(value, float):
            value = round(value, 3)
        elif isinstance(value, list):
            value = tuple(value)
        values.append((key, value))
    return tuple(values)


def _set_continuous(state, field, event):
    previous = getattr(state, field, {}) or {}
    setattr(state, field, event)
    if _continuous_fingerprint(previous) != _continuous_fingerprint(event):
        state.continuous_evidence_revision = int(
            getattr(state, 'continuous_evidence_revision', 0)
        ) + 1


def _mid_price(state):
    bid = float(getattr(state, 'best_bid', 0.0) or 0.0)
    ask = float(getattr(state, 'best_ask', 0.0) or 0.0)
    return (bid + ask) / 2.0 if ask > 0.0 and bid > 0.0 else max(bid, ask)


def _set_event(state, field, event):
    previous = getattr(state, field, {}) or {}
    changed = (
        previous.get('active') != event.get('active')
        or previous.get('event_id') != event.get('event_id')
        or previous.get('direction') != event.get('direction')
        or previous.get('blocked_bias') != event.get('blocked_bias')
    )
    setattr(state, field, event)
    if changed:
        state.decision_revision = int(getattr(state, 'decision_revision', 0)) + 1


def _inactive(state, field, now, reason):
    current = getattr(state, field, {}) or {}
    if current.get('active'):
        _set_event(state, field, {
            'active': False, 'direction': None, 'blocked_bias': None,
            'ts': now, 'event_id': None, 'reason': reason,
        })


def _activate_directional(state, field, now, direction, payload):
    current = getattr(state, field, {}) or {}
    ttl = float(payload.get('ttl', FLOW_EVENT_TTL) or FLOW_EVENT_TTL)
    reusable = (
        current.get('active') and current.get('direction') == direction
        and now - float(current.get('ts', 0.0)) <= ttl
    )
    if reusable:
        event_id = current.get('event_id')
    else:
        state.trend_context_sequence += 1
        event_id = f"{field}:{state.trend_context_sequence}:{direction}"
    event = {
        'active': True, 'direction': direction, 'ts': now,
        'event_id': event_id, **payload,
    }
    _set_event(state, field, event)


def _activate_trap(state, field, now, blocked_bias, payload):
    current = getattr(state, field, {}) or {}
    ttl = float(payload.get('ttl', TRAP_EVENT_TTL) or TRAP_EVENT_TTL)
    reusable = (
        current.get('active') and current.get('blocked_bias') == blocked_bias
        and (
            payload.get('persistent_latch')
            or now - float(current.get('ts', 0.0)) <= ttl
        )
    )
    if reusable:
        event_id = current.get('event_id')
    else:
        state.trend_context_sequence += 1
        event_id = f"{field}:{state.trend_context_sequence}:{blocked_bias}"
    _set_event(state, field, {
        'active': True, 'blocked_bias': blocked_bias, 'ts': now,
        'event_id': event_id, **payload,
    })


def _sample_price(state, now, price):
    history = state.trend_price_history
    if not history or now - history[-1]['ts'] >= PRICE_SAMPLE_INTERVAL:
        history.append({'ts': now, 'price': price})
    cutoff = now - 190.0
    while history and history[0]['ts'] < cutoff:
        history.popleft()


def _price_at(state, target):
    history = state.trend_price_history
    if not history:
        return None
    return min(history, key=lambda item: abs(item['ts'] - target))


def _flow_window(buffer, cutoff):
    buy = sum(float(item.get('buy', 0.0)) for item in buffer if item['ts'] >= cutoff)
    sell = sum(float(item.get('sell', 0.0)) for item in buffer if item['ts'] >= cutoff)
    total = buy + sell
    imbalance = (buy - sell) / total if total > 0.0 else 0.0
    return buy, sell, total, imbalance


def _update_persistent_flow(state, now, price, atr):
    buffer = state.flow_1s_buffer
    cutoff = int(now) - 190
    while buffer and buffer[0]['ts'] < cutoff:
        buffer.popleft()
    if not buffer or buffer[0]['ts'] > int(now) - 45:
        coverage = max(0.0, now - float(buffer[0]['ts'])) if buffer else 0.0
        _set_continuous(state, 'continuous_persistent_flow', {
            'active': False, 'direction': 0.0, 'strength': 0.0,
            'quality': _clamp(coverage / 45.0), 'ts': now,
            'ttl': FLOW_EVENT_TTL, 'coverage_seconds': coverage,
            'source_event_id': None, 'source_family': 'AGGTRADE',
            'dependency_families': ['AGGTRADE'],
            'quality_flags': ['WARMUP_LT_45S'],
        })
        _inactive(state, 'persistent_flow', now, 'WARMUP_LT_45S')
        _inactive(state, 'flow_price_trap', now, 'WARMUP_LT_45S')
        return

    buy15, sell15, volume15, imbalance15 = _flow_window(
        buffer, int(now) - FLOW_FAST_SECONDS + 1
    )
    buy60, sell60, volume60, imbalance60 = _flow_window(
        buffer, int(now) - FLOW_SLOW_SECONDS + 1
    )
    minimum_volume15 = max(
        2.0, 2.0 * float(getattr(state, 'vol_pct90', 0.0) or 0.0)
    )
    direction = None
    if (
        imbalance15 >= FLOW_FAST_IMBALANCE_MIN
        and imbalance60 >= FLOW_SLOW_IMBALANCE_MIN
    ):
        direction = 'LONG'
    elif (
        imbalance15 <= -FLOW_FAST_IMBALANCE_MIN
        and imbalance60 <= -FLOW_SLOW_IMBALANCE_MIN
    ):
        direction = 'SHORT'

    bucket_signs = []
    for start in (int(now) - 14, int(now) - 9, int(now) - 4):
        _, _, total, imbalance = _flow_window(
            [item for item in buffer if start <= item['ts'] <= start + 4], start
        )
        bucket_signs.append(imbalance if total > 0.0 else 0.0)
    sign = 1.0 if direction == 'LONG' else -1.0 if direction == 'SHORT' else 0.0
    persistence = sum(
        1 for imbalance in bucket_signs
        if sign and imbalance * sign >= FLOW_BUCKET_IMBALANCE_MIN
    )
    start = _price_at(state, now - FLOW_FAST_SECONDS)
    coverage = now - float(start['ts']) if start else 0.0
    price_progress_atr = (
        (price - float(start['price'])) / atr if start and atr > 0.0 else 0.0
    )
    aligned_progress = price_progress_atr * sign
    base_payload = {
        'buy_15s': buy15, 'sell_15s': sell15, 'volume_15s': volume15,
        'imbalance_15s': imbalance15, 'buy_60s': buy60, 'sell_60s': sell60,
        'volume_60s': volume60, 'imbalance_60s': imbalance60,
        'persistence_buckets': persistence,
        'price_progress_atr_15s': price_progress_atr,
        'minimum_volume_15s': minimum_volume15,
        'coverage_seconds': coverage,
        'ttl': FLOW_EVENT_TTL,
    }
    raw_imbalance = 0.65 * imbalance15 + 0.35 * imbalance60
    raw_direction = 1.0 if raw_imbalance > 0.0 else -1.0 if raw_imbalance < 0.0 else 0.0
    materiality = _clamp(volume15 / max(minimum_volume15, 1e-9))
    persistence_quality = _clamp(persistence / 3.0)
    flow_strength = _clamp(
        materiality * (
            0.55 * _clamp(abs(imbalance15) / 0.50)
            + 0.25 * _clamp(abs(imbalance60) / 0.30)
            + 0.20 * persistence_quality
        )
    )
    flow_side = 'LONG' if raw_direction > 0.0 else 'SHORT' if raw_direction < 0.0 else None
    _set_continuous(state, 'continuous_persistent_flow', {
        'active': bool(raw_direction and flow_strength > 0.0),
        'direction': raw_direction,
        'strength': flow_strength,
        'quality': _clamp(0.50 * materiality + 0.30 * persistence_quality + 0.20 * _clamp(coverage / 15.0)),
        'ts': now, 'ttl': FLOW_EVENT_TTL,
        'source_event_id': f"flow-window:{int(now)}:{flow_side}",
        'source_family': 'AGGTRADE',
        'dependency_families': ['AGGTRADE'],
        'price_progress_atr_15s': price_progress_atr,
        'aligned_price_progress_atr': price_progress_atr * raw_direction,
        'materiality': materiality,
        **base_payload,
    })
    qualified_flow = (
        direction is not None and volume15 >= minimum_volume15
        and persistence >= 2 and coverage >= 12.0
    )
    if qualified_flow and aligned_progress >= FLOW_PRICE_CONFIRM_ATR:
        _activate_directional(
            state, 'persistent_flow', now, direction,
            {'classification': 'PERSISTENT_FLOW_PRICE_CONFIRMED', **base_payload},
        )
        _inactive(state, 'flow_price_trap', now, 'PRICE_CONFIRMED')
    else:
        _inactive(state, 'persistent_flow', now, 'FLOW_NOT_CONFIRMED')
        if qualified_flow and aligned_progress <= -FLOW_PRICE_TRAP_ATR:
            _activate_trap(
                state, 'flow_price_trap', now, direction,
                {'classification': 'AGGRESSIVE_FLOW_FAILED_PRICE', **base_payload},
            )
        else:
            _inactive(state, 'flow_price_trap', now, 'NO_TRAP')


def _trend_pullback_context(state):
    mode = getattr(state, 'current_mode', {}) or {}
    modes = mode.get('modes', [])
    if not any(name in modes for name in ('TREND-PULLBACK', 'TRANSITION-PULLBACK')):
        return None, []
    bias = mode.get('bias')
    zones = []
    for value in mode.get('pullback_zones', []):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0.0:
            zones.append(parsed)
    return (bias if bias in ('LONG', 'SHORT') else None), zones


def _update_zone_reaction(state, now, price, atr):
    bias, zones = _trend_pullback_context(state)
    if bias is None or not zones:
        state.trend_zone_probe = None
        _set_continuous(state, 'continuous_zone_reaction', {
            'active': False, 'direction': 0.0, 'strength': 0.0,
            'quality': 0.0, 'ts': now, 'ttl': ZONE_EVENT_TTL,
            'source_event_id': None, 'source_family': 'PRICE_REACTION',
            'dependency_families': ['PRICE_REACTION'],
        })
        _inactive(state, 'zone_acceptance_trap', now, 'PULLBACK_CONTEXT_ENDED')
        return

    # Acceptance là trạng thái cấu trúc, không phải xung 8 giây. Khi
    # giá đã nằm sai phía VAH/VAL/POC đủ lâu, giữ cờ cho tới khi
    # reclaim có buffer hoặc setup/mode thật sự thay đổi.
    active_trap = getattr(state, 'zone_acceptance_trap', {}) or {}
    if active_trap.get('active'):
        trapped_zone = float(active_trap.get('zone', 0.0) or 0.0)
        same_setup = (
            active_trap.get('blocked_bias') == bias
            and trapped_zone > 0.0
            and any(abs(value - trapped_zone) <= 0.25 * atr for value in zones)
        )
        if not same_setup:
            _inactive(state, 'zone_acceptance_trap', now, 'ZONE_OR_BIAS_CHANGED')
        else:
            reclaimed = (
                price >= trapped_zone + ZONE_RECLAIM_ATR * atr
                if bias == 'LONG'
                else price <= trapped_zone - ZONE_RECLAIM_ATR * atr
            )
            if not reclaimed:
                accepted_at = float(
                    active_trap.get('accepted_at', active_trap.get('ts', now))
                    or now
                )
                adverse = (
                    (trapped_zone - price) / atr
                    if bias == 'LONG'
                    else (price - trapped_zone) / atr
                )
                _activate_trap(state, 'zone_acceptance_trap', now, bias, {
                    'classification': 'VALUE_ACCEPTED_THROUGH_ZONE',
                    'zone': trapped_zone,
                    'adverse_atr': adverse,
                    'accepted_at': accepted_at,
                    'accepted_seconds': max(0.0, now - accepted_at),
                    'persistent_latch': True,
                    'ttl': TRAP_EVENT_TTL,
                })
                _set_continuous(state, 'continuous_zone_reaction', {
                    'active': True,
                    'direction': 1.0 if bias == 'LONG' else -1.0,
                    'strength': 0.0,
                    'quality': _clamp(abs(adverse) / ZONE_INVALIDATION_ATR),
                    'ts': now, 'ttl': ZONE_EVENT_TTL,
                    'source_event_id': active_trap.get('event_id'),
                    'source_family': 'PRICE_REACTION',
                    'dependency_families': ['PRICE_REACTION'],
                    'zone': trapped_zone, 'favorable_atr': 0.0,
                    'adverse_atr': adverse,
                    'accepted_seconds': max(0.0, now - accepted_at),
                    'classification': 'VALUE_ACCEPTED_THROUGH_ZONE',
                })
                state.trend_zone_probe = None
                return
            _inactive(state, 'zone_acceptance_trap', now, 'ZONE_RECLAIMED')

    zone = min(zones, key=lambda value: abs(price - value))
    probe = state.trend_zone_probe
    if probe is not None and (
        probe.get('bias') != bias
        or abs(float(probe.get('zone', 0.0)) - zone) > 0.25 * atr
    ):
        probe = None
        state.trend_zone_probe = None

    if probe is None and abs(price - zone) <= ZONE_TOUCH_ATR * atr:
        probe = {
            'bias': bias, 'zone': zone, 'started_at': now,
            'min_price': price, 'max_price': price,
            'acceptance_since': 0.0,
        }
        state.trend_zone_probe = probe
    if probe is None:
        _set_continuous(state, 'continuous_zone_reaction', {
            'active': False, 'direction': 0.0, 'strength': 0.0,
            'quality': 0.0, 'ts': now, 'ttl': ZONE_EVENT_TTL,
            'source_event_id': None, 'source_family': 'PRICE_REACTION',
            'dependency_families': ['PRICE_REACTION'],
        })
        return

    probe['min_price'] = min(float(probe['min_price']), price)
    probe['max_price'] = max(float(probe['max_price']), price)
    age = now - float(probe['started_at'])
    zone = float(probe['zone'])
    favorable = (price - zone) / atr if bias == 'LONG' else (zone - price) / atr
    adverse = (zone - price) / atr if bias == 'LONG' else (price - zone) / atr
    max_adverse = (
        (zone - float(probe['min_price'])) / atr
        if bias == 'LONG'
        else (float(probe['max_price']) - zone) / atr
    )
    if adverse >= ZONE_ACCEPTANCE_ATR:
        probe['acceptance_since'] = float(probe.get('acceptance_since', 0.0) or now)
    else:
        probe['acceptance_since'] = 0.0
    accepted_for = (
        now - float(probe['acceptance_since']) if probe['acceptance_since'] else 0.0
    )
    _set_continuous(state, 'continuous_zone_reaction', {
        'active': True,
        'direction': 1.0 if bias == 'LONG' else -1.0,
        'strength': _clamp(max(0.0, favorable) / ZONE_REJECTION_ATR),
        'quality': _clamp(
            0.45 * _clamp(age / ZONE_MIN_AGE)
            + 0.35 * _clamp(1.0 - max(0.0, max_adverse) / ZONE_INVALIDATION_ATR)
            + 0.20 * _clamp(max(0.0, favorable) / ZONE_REJECTION_ATR)
        ),
        'ts': now, 'ttl': ZONE_EVENT_TTL,
        'source_event_id': f"zone-probe:{int(float(probe['started_at']) * 10)}:{bias}:{zone:.2f}",
        'source_family': 'PRICE_REACTION',
        'dependency_families': ['PRICE_REACTION'],
        'zone': zone, 'age': age, 'favorable_atr': favorable,
        'adverse_atr': adverse, 'max_adverse_atr': max(0.0, max_adverse),
        'accepted_seconds': accepted_for,
    })
    if accepted_for >= ZONE_ACCEPTANCE_SECONDS:
        _activate_trap(state, 'zone_acceptance_trap', now, bias, {
            'classification': 'VALUE_ACCEPTED_THROUGH_ZONE',
            'zone': zone, 'adverse_atr': adverse,
            'accepted_at': float(probe['acceptance_since']),
            'accepted_seconds': accepted_for, 'persistent_latch': True,
            'ttl': TRAP_EVENT_TTL,
        })
        state.trend_zone_probe = None
        return
    if age >= ZONE_MIN_AGE and favorable >= ZONE_REJECTION_ATR:
        _activate_directional(state, 'zone_reaction', now, bias, {
            'classification': 'POC_VA_REJECTION_CONFIRMED',
            'zone': zone, 'age': age, 'displacement_atr': favorable,
            'max_adverse_atr': max(0.0, max_adverse), 'ttl': ZONE_EVENT_TTL,
        })
        _inactive(state, 'zone_acceptance_trap', now, 'ZONE_REJECTED')
        state.trend_zone_probe = None
        return
    if age > ZONE_MAX_AGE or max_adverse >= ZONE_INVALIDATION_ATR:
        state.trend_zone_probe = None


def update(state, now=None):
    """Cập nhật tối đa 5Hz; hot path O(60), mọi buffer đều bounded."""
    now = time.time() if now is None else float(now)
    last = float(getattr(state, 'trend_context_last_update', 0.0) or 0.0)
    if now - last < UPDATE_INTERVAL:
        return
    state.trend_context_last_update = now
    price = _mid_price(state)
    atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
    if price <= 0.0 or atr <= 0.0:
        return
    _sample_price(state, now, price)
    _update_persistent_flow(state, now, price, atr)
    _update_zone_reaction(state, now, price, atr)
