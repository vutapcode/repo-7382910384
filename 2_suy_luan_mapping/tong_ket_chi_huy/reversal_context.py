"""Ngữ cảnh đảo chiều cho NEUTRAL-FADE, cập nhật từ dữ liệu đã có trong RAM."""

import time


UPDATE_INTERVAL = 0.05
FLOW_SAMPLE_INTERVAL = 0.5
FLOW_LOOKBACK_SECONDS = 8.0
FLOW_IMBALANCE_MIN = 0.30
FLOW_PRICE_PROGRESS_MAX_ATR = 0.12
FLOW_EVENT_TTL = 2.0

ABSORPTION_MIN_AGE = 1.0
ABSORPTION_MAX_AGE = 5.0
ABSORPTION_FAVORABLE_ATR = 0.05
ABSORPTION_BREAK_ATR = 0.30

ZONE_SWEEP_EXCURSION_ATR = 0.05
ZONE_SWEEP_RECLAIM_ATR = 0.02
ZONE_SWEEP_MAX_AGE = 20.0


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
    return (bid + ask) / 2.0 if bid > 0.0 and ask > 0.0 else max(bid, ask)


def _set_event(state, field, event):
    previous = getattr(state, field, {}) or {}
    changed = (
        previous.get('event_id') != event.get('event_id')
        or previous.get('active') != event.get('active')
        or previous.get('classification') != event.get('classification')
    )
    setattr(state, field, event)
    if changed:
        state.decision_revision = getattr(state, 'decision_revision', 0) + 1


def _update_flow_divergence(state, now, price, atr):
    last_sample = float(getattr(state, 'flow_price_last_sample', 0.0) or 0.0)
    if now - last_sample < FLOW_SAMPLE_INTERVAL:
        return
    state.flow_price_last_sample = now
    history = state.flow_price_history
    history.append({
        'ts': now,
        'price': price,
        'buy_total': float(getattr(state, 'cvd_buy', 0.0) or 0.0),
        'sell_total': float(getattr(state, 'cvd_sell', 0.0) or 0.0),
    })
    cutoff = now - 30.0
    while history and history[0]['ts'] < cutoff:
        history.popleft()
    if not history or now - history[0]['ts'] < FLOW_LOOKBACK_SECONDS:
        return

    target = now - FLOW_LOOKBACK_SECONDS
    start = min(history, key=lambda item: abs(item['ts'] - target))
    buy_delta = max(0.0, history[-1]['buy_total'] - start['buy_total'])
    sell_delta = max(0.0, history[-1]['sell_total'] - start['sell_total'])
    total = buy_delta + sell_delta
    minimum_volume = max(
        0.25,
        float(getattr(state, 'vol_pct90', 0.0) or 0.0),
    )
    if total < minimum_volume:
        _set_continuous(state, 'continuous_flow_divergence', {
            'active': False, 'direction': 0.0, 'strength': 0.0,
            'quality': _clamp(total / max(minimum_volume, 1e-9)),
            'ts': now, 'ttl': FLOW_EVENT_TTL,
            'source_event_id': f"flowdiv-window:{int(start['ts'])}",
            'source_family': 'AGGTRADE_PRICE',
            'dependency_families': ['AGGTRADE', 'PRICE_REACTION'],
            'total_volume': total, 'minimum_volume': minimum_volume,
            'quality_flags': ['INSUFFICIENT_FLOW'],
        })
        _set_event(state, 'flow_divergence', {
            'active': False, 'direction': None, 'ts': now,
            'event_id': None, 'reason': 'INSUFFICIENT_FLOW',
        })
        return

    imbalance = (buy_delta - sell_delta) / total
    price_progress_atr = (price - start['price']) / atr
    aggression_direction = 1.0 if imbalance > 0.0 else -1.0 if imbalance < 0.0 else 0.0
    reversal_direction = -aggression_direction
    aligned_progress = price_progress_atr * aggression_direction
    failure_strength = _clamp(
        (FLOW_PRICE_PROGRESS_MAX_ATR - aligned_progress)
        / max(FLOW_PRICE_PROGRESS_MAX_ATR * 2.0, 1e-9)
    )
    materiality = _clamp(total / max(minimum_volume, 1e-9))
    _set_continuous(state, 'continuous_flow_divergence', {
        'active': bool(aggression_direction and failure_strength > 0.0),
        'direction': reversal_direction,
        'strength': _clamp(
            _clamp(abs(imbalance) / 0.60) * failure_strength * materiality
        ),
        'quality': _clamp(0.55 * materiality + 0.45 * _clamp((now - start['ts']) / FLOW_LOOKBACK_SECONDS)),
        'ts': now, 'ttl': FLOW_EVENT_TTL,
        'source_event_id': f"flowdiv-window:{int(start['ts'])}",
        'source_family': 'AGGTRADE_PRICE',
        'dependency_families': ['AGGTRADE', 'PRICE_REACTION'],
        'imbalance': imbalance, 'total_volume': total,
        'minimum_volume': minimum_volume,
        'price_progress_atr': price_progress_atr,
        'aligned_aggression_progress_atr': aligned_progress,
    })
    direction = None
    if (
        imbalance <= -FLOW_IMBALANCE_MIN
        and price_progress_atr >= -FLOW_PRICE_PROGRESS_MAX_ATR
    ):
        direction = 'LONG'
    elif (
        imbalance >= FLOW_IMBALANCE_MIN
        and price_progress_atr <= FLOW_PRICE_PROGRESS_MAX_ATR
    ):
        direction = 'SHORT'

    event_id = (
        f"flowdiv:{int(start['ts'])}:{direction}" if direction is not None else None
    )
    _set_event(state, 'flow_divergence', {
        'active': direction is not None,
        'direction': direction,
        'ts': now,
        'event_id': event_id,
        'lookback_seconds': now - start['ts'],
        'imbalance': imbalance,
        'price_progress_atr': price_progress_atr,
        'ttl': FLOW_EVENT_TTL,
    })


def _track_new_absorption(state, now, price, atr):
    raw = getattr(state, 'absorption_event', {}) or {}
    event_id = raw.get('event_id')
    if not raw.get('active') or not event_id:
        return
    if any(item['source_event_id'] == event_id for item in state.absorption_trackers):
        return
    reference = float(raw.get('reference_price', 0.0) or price)
    event_atr = float(raw.get('atr_at_event', 0.0) or atr)
    state.absorption_trackers.append({
        'source_event_id': event_id,
        'side': raw.get('side'),
        'started_at': float(raw.get('ts', now) or now),
        'reference_price': reference,
        'atr': max(event_atr, 1e-9),
        'min_price': min(reference, price),
        'max_price': max(reference, price),
    })


def _update_absorption_reaction(state, now, price):
    survivors = []
    continuous_candidates = []
    for tracker in state.absorption_trackers:
        tracker['min_price'] = min(tracker['min_price'], price)
        tracker['max_price'] = max(tracker['max_price'], price)
        age = now - tracker['started_at']
        if age > ABSORPTION_MAX_AGE:
            continue
        survivors.append(tracker)
        reference = tracker['reference_price']
        atr = tracker['atr']
        if tracker['side'] == 'buy':
            direction = 'LONG'
            favorable = (tracker['max_price'] - reference) / atr
            adverse = (reference - tracker['min_price']) / atr
        elif tracker['side'] == 'sell':
            direction = 'SHORT'
            favorable = (reference - tracker['min_price']) / atr
            adverse = (tracker['max_price'] - reference) / atr
        else:
            continue

        continuous_candidates.append({
            'active': True,
            'direction': 1.0 if direction == 'LONG' else -1.0,
            'strength': _clamp(
                max(0.0, favorable) / ABSORPTION_FAVORABLE_ATR
            ) * _clamp(1.0 - max(0.0, adverse) / ABSORPTION_BREAK_ATR),
            'quality': _clamp(
                0.50 * _clamp(age / ABSORPTION_MIN_AGE)
                + 0.50 * _clamp(1.0 - max(0.0, adverse) / ABSORPTION_BREAK_ATR)
            ),
            'ts': now, 'ttl': ABSORPTION_MAX_AGE,
            'source_event_id': f"absreact:{tracker['source_event_id']}:{direction}",
            'parent_event_id': tracker['source_event_id'],
            'source_family': 'DEPTH_AGGTRADE_PRICE',
            'dependency_families': ['DEPTH', 'AGGTRADE', 'PRICE_REACTION'],
            'age': age, 'favorable_atr': favorable, 'adverse_atr': adverse,
        })

        if adverse >= ABSORPTION_BREAK_ATR:
            current = getattr(state, 'absorption_reaction', {}) or {}
            if current.get('source_event_id') == tracker['source_event_id']:
                _set_event(state, 'absorption_reaction', {
                    'active': False,
                    'classification': 'AGGRESSIVE_BREAK',
                    'direction': direction,
                    'ts': now,
                    'event_id': None,
                    'source_event_id': tracker['source_event_id'],
                    'favorable_atr': favorable,
                    'adverse_atr': adverse,
                })
            continue
        if age >= ABSORPTION_MIN_AGE and favorable >= ABSORPTION_FAVORABLE_ATR:
            _set_event(state, 'absorption_reaction', {
                'active': True,
                'classification': 'PASSIVE_HOLD_REACTION',
                'direction': direction,
                'ts': now,
                'event_id': f"absreact:{tracker['source_event_id']}:{direction}",
                'source_event_id': tracker['source_event_id'],
                'age': age,
                'favorable_atr': favorable,
                'adverse_atr': adverse,
            })
    state.absorption_trackers.clear()
    state.absorption_trackers.extend(survivors)
    if continuous_candidates:
        _set_continuous(
            state, 'continuous_absorption_reaction',
            max(continuous_candidates, key=lambda item: item['strength']),
        )
    else:
        _set_continuous(state, 'continuous_absorption_reaction', {
            'active': False, 'direction': 0.0, 'strength': 0.0,
            'quality': 0.0, 'ts': now, 'ttl': ABSORPTION_MAX_AGE,
            'source_event_id': None, 'parent_event_id': None,
            'source_family': 'DEPTH_AGGTRADE_PRICE',
            'dependency_families': ['DEPTH', 'AGGTRADE', 'PRICE_REACTION'],
        })


def _update_value_area_sweep(state, now, price, atr):
    modes = (getattr(state, 'current_mode', {}) or {}).get('modes', [])
    if 'NEUTRAL-FADE' not in modes:
        state.value_area_excursions = {'LONG': None, 'SHORT': None}
        _set_continuous(state, 'continuous_value_area_sweep', {
            'active': False, 'direction': 0.0, 'strength': 0.0,
            'quality': 0.0, 'ts': now, 'ttl': ZONE_SWEEP_MAX_AGE,
            'source_event_id': None, 'source_family': 'PRICE_REACTION',
            'dependency_families': ['PRICE_REACTION'],
        })
        return
    val = float(getattr(state, 'val', 0.0) or 0.0)
    vah = float(getattr(state, 'vah', 0.0) or 0.0)
    if val <= 0.0 or vah <= val:
        return

    excursions = state.value_area_excursions
    continuous_candidates = []
    long_item = excursions.get('LONG')
    if price <= val - ZONE_SWEEP_EXCURSION_ATR * atr:
        if long_item is None or abs(long_item['zone'] - val) > 0.25 * atr:
            excursions['LONG'] = {'started_at': now, 'zone': val, 'extreme': price}
        else:
            long_item['extreme'] = min(long_item['extreme'], price)
        item = excursions['LONG']
        continuous_candidates.append({
            'active': True, 'direction': 1.0,
            'strength': _clamp((val - item['extreme']) / atr / 0.20),
            'quality': _clamp(1.0 - (now - item['started_at']) / ZONE_SWEEP_MAX_AGE),
            'ts': now, 'ttl': ZONE_SWEEP_MAX_AGE,
            'source_event_id': f"vasweep-probe:{int(item['started_at'] * 10)}:LONG",
            'source_family': 'PRICE_REACTION',
            'dependency_families': ['PRICE_REACTION'],
            'zone': val, 'extreme': item['extreme'],
            'excursion_atr': (val - item['extreme']) / atr,
            'reclaim_atr': max(0.0, price - val) / atr,
        })
    elif long_item is not None:
        age = now - long_item['started_at']
        if age > ZONE_SWEEP_MAX_AGE:
            excursions['LONG'] = None
        elif price >= long_item['zone'] + ZONE_SWEEP_RECLAIM_ATR * atr:
            state.reversal_event_sequence += 1
            _set_event(state, 'value_area_sweep', {
                'active': True, 'direction': 'LONG', 'ts': now,
                'event_id': f"vasweep:{state.reversal_event_sequence}:LONG",
                'zone': long_item['zone'], 'extreme': long_item['extreme'],
                'age': age,
            })
            continuous_candidates.append({
                'active': True, 'direction': 1.0,
                'strength': _clamp(
                    0.55 * _clamp((long_item['zone'] - long_item['extreme']) / atr / 0.20)
                    + 0.45 * _clamp((price - long_item['zone']) / atr / 0.10)
                ),
                'quality': _clamp(1.0 - age / ZONE_SWEEP_MAX_AGE),
                'ts': now, 'ttl': ZONE_SWEEP_MAX_AGE,
                'source_event_id': f"vasweep-probe:{int(long_item['started_at'] * 10)}:LONG",
                'event_id': getattr(state, 'value_area_sweep', {}).get('event_id'),
                'source_family': 'PRICE_REACTION',
                'dependency_families': ['PRICE_REACTION'],
                'zone': long_item['zone'], 'extreme': long_item['extreme'],
                'excursion_atr': (long_item['zone'] - long_item['extreme']) / atr,
                'reclaim_atr': (price - long_item['zone']) / atr,
            })
            excursions['LONG'] = None

    short_item = excursions.get('SHORT')
    if price >= vah + ZONE_SWEEP_EXCURSION_ATR * atr:
        if short_item is None or abs(short_item['zone'] - vah) > 0.25 * atr:
            excursions['SHORT'] = {'started_at': now, 'zone': vah, 'extreme': price}
        else:
            short_item['extreme'] = max(short_item['extreme'], price)
        item = excursions['SHORT']
        continuous_candidates.append({
            'active': True, 'direction': -1.0,
            'strength': _clamp((item['extreme'] - vah) / atr / 0.20),
            'quality': _clamp(1.0 - (now - item['started_at']) / ZONE_SWEEP_MAX_AGE),
            'ts': now, 'ttl': ZONE_SWEEP_MAX_AGE,
            'source_event_id': f"vasweep-probe:{int(item['started_at'] * 10)}:SHORT",
            'source_family': 'PRICE_REACTION',
            'dependency_families': ['PRICE_REACTION'],
            'zone': vah, 'extreme': item['extreme'],
            'excursion_atr': (item['extreme'] - vah) / atr,
            'reclaim_atr': max(0.0, vah - price) / atr,
        })
    elif short_item is not None:
        age = now - short_item['started_at']
        if age > ZONE_SWEEP_MAX_AGE:
            excursions['SHORT'] = None
        elif price <= short_item['zone'] - ZONE_SWEEP_RECLAIM_ATR * atr:
            state.reversal_event_sequence += 1
            _set_event(state, 'value_area_sweep', {
                'active': True, 'direction': 'SHORT', 'ts': now,
                'event_id': f"vasweep:{state.reversal_event_sequence}:SHORT",
                'zone': short_item['zone'], 'extreme': short_item['extreme'],
                'age': age,
            })
            continuous_candidates.append({
                'active': True, 'direction': -1.0,
                'strength': _clamp(
                    0.55 * _clamp((short_item['extreme'] - short_item['zone']) / atr / 0.20)
                    + 0.45 * _clamp((short_item['zone'] - price) / atr / 0.10)
                ),
                'quality': _clamp(1.0 - age / ZONE_SWEEP_MAX_AGE),
                'ts': now, 'ttl': ZONE_SWEEP_MAX_AGE,
                'source_event_id': f"vasweep-probe:{int(short_item['started_at'] * 10)}:SHORT",
                'event_id': getattr(state, 'value_area_sweep', {}).get('event_id'),
                'source_family': 'PRICE_REACTION',
                'dependency_families': ['PRICE_REACTION'],
                'zone': short_item['zone'], 'extreme': short_item['extreme'],
                'excursion_atr': (short_item['extreme'] - short_item['zone']) / atr,
                'reclaim_atr': (short_item['zone'] - price) / atr,
            })
            excursions['SHORT'] = None

    if continuous_candidates:
        _set_continuous(
            state, 'continuous_value_area_sweep',
            max(continuous_candidates, key=lambda item: item['strength']),
        )
    else:
        _set_continuous(state, 'continuous_value_area_sweep', {
            'active': False, 'direction': 0.0, 'strength': 0.0,
            'quality': 0.0, 'ts': now, 'ttl': ZONE_SWEEP_MAX_AGE,
            'source_event_id': None, 'source_family': 'PRICE_REACTION',
            'dependency_families': ['PRICE_REACTION'],
        })


def update(state, now=None):
    """Cập nhật tối đa 20Hz; mọi cấu trúc đều bounded để không tăng RAM vô hạn."""
    now = time.time() if now is None else float(now)
    last = float(getattr(state, 'reversal_last_update', 0.0) or 0.0)
    if now - last < UPDATE_INTERVAL:
        return
    state.reversal_last_update = now
    price = _mid_price(state)
    atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
    if price <= 0.0 or atr <= 0.0:
        return
    _update_flow_divergence(state, now, price, atr)
    _track_new_absorption(state, now, price, atr)
    _update_absorption_reaction(state, now, price)
    _update_value_area_sweep(state, now, price, atr)
