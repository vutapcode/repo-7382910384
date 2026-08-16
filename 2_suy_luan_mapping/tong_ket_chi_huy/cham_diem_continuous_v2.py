"""CONTINUOUS_V2: causal momentum/reversal scorer, pure va deterministic."""

import math


LIVE_VERSION = 'CONTINUOUS_V2'
MIN_TARGET_NOTIONAL_PCT = 0.30
MAX_TARGET_NOTIONAL_PCT = 9.0
NEUTRAL_MOMENTUM_MAX_PCT = 1.0
HORIZONS = (15, 60, 180)
ADVERSE_FLOW_MEMORY_HALF_LIFE_SECONDS = 5.0


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def _number(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _sigmoid(value):
    value = float(value)
    if value >= 0.0:
        decay = math.exp(-value)
        return 1.0 / (1.0 + decay)
    growth = math.exp(value)
    return growth / (1.0 + growth)


def _smoothstep(edge0, edge1, value):
    if edge1 <= edge0:
        return 0.0
    x = _clamp((float(value) - edge0) / (edge1 - edge0))
    return x * x * (3.0 - 2.0 * x)


def _side_sign(side):
    return 1.0 if side == 'LONG' else -1.0


def _event(snapshot, name):
    value = getattr(snapshot, name, {}) or {}
    return dict(value) if isinstance(value, dict) else {}


def _freshness(event, now, default_ttl):
    timestamp = _number(event.get('ts'))
    ttl = max(_number(event.get('ttl'), default_ttl), 1e-6)
    age = now - timestamp
    if timestamp <= 0.0 or age < -0.25:
        return 0.0
    return _clamp(1.0 - max(0.0, age) / ttl)


def _mode_family(mode):
    mode = str(mode or '').upper()
    if mode == 'NEUTRAL-MOMENTUM':
        return 'NEUTRAL_MOMENTUM'
    if 'BREAKOUT' in mode:
        return 'BREAKOUT'
    if 'PULLBACK' in mode:
        return 'TREND_PULLBACK'
    if 'FADE' in mode or 'NEUTRAL' in mode:
        return 'LIQUIDITY_REVERSAL'
    return 'BALANCED'


def _continuous_item(name, group, event, now, ttl, activation=False):
    direction = _clamp(_number(event.get('direction')), -1.0, 1.0)
    strength = _clamp(_number(event.get('strength')))
    quality = _clamp(_number(event.get('quality'), 0.5 if strength else 0.0))
    dependencies = [str(value) for value in event.get('dependency_families', ()) or ()]
    family = event.get('source_family')
    if family and str(family) not in dependencies:
        dependencies.append(str(family))
    source_id = event.get('source_event_id') or event.get('event_id')
    parent_id = event.get('parent_event_id') or source_id
    return {
        'name': name, 'group': group, 'direction': direction,
        'strength': strength, 'quality': quality,
        'freshness': _freshness(event, now, ttl),
        'activation_source': bool(activation), 'ts': _number(event.get('ts')),
        'source_event_id': source_id, 'parent_event_id': parent_id,
        'dependencies': dependencies,
        'metrics': {
            key: value for key, value in event.items()
            if key not in {
                'active', 'direction', 'strength', 'quality', 'freshness',
                'source_event_id', 'event_id', 'parent_event_id',
                'source_family', 'dependency_families', 'ts', 'ttl',
            }
        },
    }


def _normalized_momentum(snapshot):
    source = getattr(snapshot, 'momentum_horizons', {}) or {}
    scales = {15: 0.18, 60: 0.45, 180: 0.90}
    price_weights = {15: 0.20, 60: 0.35, 180: 0.45}
    flow_weights = {15: 0.35, 60: 0.35, 180: 0.30}
    price_momentum = 0.0
    flow_momentum = 0.0
    price_weight = 0.0
    flow_weight = 0.0
    acceptance_long = 0.0
    acceptance_short = 0.0
    breakdown = {}
    p90 = max(_number(getattr(snapshot, 'vol_pct90', 0.0)), 1e-9)
    for horizon in HORIZONS:
        row = dict(source.get(str(horizon), {}) or {})
        price_coverage = _clamp(_number(row.get('price_coverage_seconds')) / horizon)
        flow_coverage = _clamp(_number(row.get('flow_coverage_seconds')) / horizon)
        progress = _number(row.get('price_progress_atr'))
        expansion = max(0.0, _number(row.get('range_expansion_atr')))
        efficiency = _clamp(_number(row.get('price_efficiency')))
        # Progress is directional; expansion/efficiency only control reliability.
        price_value = math.tanh(progress / scales[horizon])
        price_quality = price_coverage * _clamp(0.45 + 0.35 * efficiency + 0.20 * expansion)
        price_momentum += price_weights[horizon] * price_value * price_quality
        price_weight += price_weights[horizon] * price_coverage

        imbalance = _clamp(_number(row.get('flow_imbalance')), -1.0, 1.0)
        total = max(0.0, _number(row.get('flow_total')))
        expected = p90 * max(1.0, horizon / 3.0)
        materiality = _clamp(total / expected)
        flow_value = imbalance * flow_coverage * materiality
        flow_momentum += flow_weights[horizon] * flow_value
        flow_weight += flow_weights[horizon] * flow_coverage

        acceptance_long += price_weights[horizon] * _clamp(row.get('acceptance_long', 0.0)) * price_coverage
        acceptance_short += price_weights[horizon] * _clamp(row.get('acceptance_short', 0.0)) * price_coverage
        breakdown[str(horizon)] = {
            'price_progress_atr': round(progress, 6),
            'range_expansion_atr': round(expansion, 6),
            'price_efficiency': round(efficiency, 6),
            'price_normalized': round(price_value, 6),
            'price_coverage': round(price_coverage, 6),
            'flow_imbalance': round(imbalance, 6),
            'flow_materiality': round(materiality, 6),
            'flow_coverage': round(flow_coverage, 6),
            'acceptance_long': round(_clamp(row.get('acceptance_long', 0.0)), 6),
            'acceptance_short': round(_clamp(row.get('acceptance_short', 0.0)), 6),
        }
    price_momentum = _clamp(price_momentum, -1.0, 1.0)
    flow_momentum = _clamp(flow_momentum, -1.0, 1.0)
    acceptance = _clamp(acceptance_long - acceptance_short, -1.0, 1.0)
    state = _clamp(
        0.50 * price_momentum + 0.30 * flow_momentum + 0.20 * acceptance,
        -1.0, 1.0,
    )
    coverage = _clamp(0.55 * price_weight + 0.45 * flow_weight)
    return {
        'price': price_momentum, 'flow': flow_momentum,
        'acceptance': acceptance, 'state': state, 'coverage': coverage,
        'acceptance_long': _clamp(acceptance_long),
        'acceptance_short': _clamp(acceptance_short),
        'horizons': breakdown,
    }


def _recent_adverse_flow(snapshot, side, now):
    registry = getattr(snapshot, 'adverse_flow_memory_by_bias', {}) or {}
    record = dict(registry.get(side) or {}) if isinstance(registry, dict) else {}
    timestamp = _number(record.get('ts'))
    age = now - timestamp
    if (
        not record or record.get('blocked_bias') != side
        or timestamp <= 0.0 or age < -0.25
    ):
        return {
            'active_strength': 0.0, 'age_seconds': None,
            'source_event_id': None, 'base_severity': 0.0,
        }
    base = _clamp(_number(record.get('severity')))
    decay = math.exp(
        -math.log(2.0) * max(0.0, age)
        / ADVERSE_FLOW_MEMORY_HALF_LIFE_SECONDS
    )
    return {
        'active_strength': _clamp(base * decay),
        'age_seconds': max(0.0, age),
        'source_event_id': record.get('source_event_id'),
        'base_severity': base,
        'adverse_share': _clamp(_number(record.get('adverse_share'))),
        'net_adverse_qty': max(0.0, _number(record.get('net_adverse_qty'))),
        'contract_version': record.get('contract_version'),
        'half_life_seconds': ADVERSE_FLOW_MEMORY_HALF_LIFE_SECONDS,
    }


def _location_modifier(snapshot, setup, side, price, include_poc=True):
    atr = max(_number(getattr(snapshot, 'atr_1m', 0.0)), 1e-9)
    zone = _number(setup.get('zone'))
    proximity = math.exp(-0.5 * (abs(price - zone) / (0.75 * atr)) ** 2) if zone > 0.0 else 0.0
    modifier = 0.90 + 0.20 * proximity
    poc = _number(getattr(snapshot, 'poc', 0.0))
    if include_poc and poc > 0.0 and price > 0.0:
        signed_distance = (poc - price) / atr * _side_sign(side)
        modifier += 0.05 * math.tanh(signed_distance / 1.25)
    return _clamp(modifier, 0.80, 1.15), {
        'zone_distance_atr': abs(price - zone) / atr if zone > 0.0 else None,
        'zone_proximity': proximity,
        'poc_distance_atr': (poc - price) / atr if poc > 0.0 else None,
    }


def _retest_fit(snapshot, setup, side, momentum, price):
    atr = max(_number(getattr(snapshot, 'atr_1m', 0.0)), 1e-9)
    zone = _number(setup.get('zone'))
    distance_atr = abs(price - zone) / atr if zone > 0.0 else 99.0
    holding = momentum['acceptance_long'] if side == 'LONG' else momentum['acceptance_short']
    neutral_momentum = str(setup.get('mode', '')).upper() == 'NEUTRAL-MOMENTUM'
    if neutral_momentum:
        # Once value acceptance is persistent, the executable passive retest
        # is the local maker BBO rather than the now-stale VA boundary. This
        # keeps distance continuous without turning a held break into a chase.
        distance_atr *= 1.0 - _clamp(holding)
    else:
        correct_side = price >= zone if side == 'LONG' else price <= zone
        holding = max(holding, 0.75 if correct_side else 0.25)
    gaussian = math.exp(-0.5 * (distance_atr / 0.55) ** 2)
    return _clamp(gaussian * _clamp(holding)), distance_atr, _clamp(holding)


def _connected(a, b):
    if a.get('parent_event_id') and a.get('parent_event_id') == b.get('parent_event_id'):
        return True
    if a.get('source_event_id') and a.get('source_event_id') == b.get('source_event_id'):
        return True
    shared = set(a.get('dependencies', ())) & set(b.get('dependencies', ()))
    return bool(shared and abs(_number(a.get('ts')) - _number(b.get('ts'))) <= 3.0)


def _causal_components(items):
    usable = [item for item in items if item['strength'] > 0.0 and item['freshness'] > 0.0]
    remaining = set(range(len(usable)))
    components = []
    while remaining:
        seed = remaining.pop()
        group = {seed}
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            linked = [index for index in remaining if _connected(usable[current], usable[index])]
            for index in linked:
                remaining.remove(index)
                group.add(index)
                frontier.append(index)
        members = [usable[index] for index in sorted(group)]
        components.append({
            'component_id': 'component:' + '|'.join(sorted(
                str(item.get('parent_event_id') or item.get('source_event_id') or item['name'])
                for item in members
            )),
            'members': members,
            'dependencies': sorted(set().union(*(set(item['dependencies']) for item in members))),
        })
    return components


def _component_effect(component, side, weights):
    sign = _side_sign(side)
    support = []
    opposition = []
    for item in component['members']:
        magnitude = (
            item['strength'] * item['quality'] * item['freshness']
            * weights.get(item['group'], 0.75)
        )
        record = (magnitude, item)
        if item['direction'] * sign > 0.0:
            support.append(record)
        elif item['direction'] * sign < 0.0:
            opposition.append(record)
    support.sort(key=lambda value: value[0], reverse=True)
    strongest = support[0][0] if support else 0.0
    corroboration = min(0.20 * strongest, 0.20 * sum(value[0] for value in support[1:]))
    positive = strongest + corroboration
    negative = sum(value[0] for value in opposition)
    activation = 0.0
    trigger_names = []
    for magnitude, item in support:
        if item['activation_source']:
            activation = max(activation, magnitude)
            trigger_names.append(item['name'])
    return positive - negative, positive, negative, activation, trigger_names


def _weights(mode_family):
    if mode_family == 'BREAKOUT':
        return {'structure': 1.00, 'reaction': 0.85, 'flow': 0.90}
    if mode_family == 'TREND_PULLBACK':
        return {'structure': 0.65, 'reaction': 1.00, 'flow': 0.75}
    if mode_family == 'NEUTRAL_MOMENTUM':
        return {'structure': 0.35, 'reaction': 0.65, 'flow': 0.70}
    return {'structure': 0.40, 'reaction': 1.10, 'flow': 0.75}


def _activation_floor(snapshot, mode_family, data_confidence, price):
    base = {
        'TREND_PULLBACK': 30.0, 'BREAKOUT': 35.0,
        'LIQUIDITY_REVERSAL': 33.0, 'NEUTRAL_MOMENTUM': 28.0,
        'BALANCED': 34.0,
    }[mode_family]
    bid = _number(getattr(snapshot, 'best_bid', 0.0))
    ask = _number(getattr(snapshot, 'best_ask', 0.0))
    spread_bps = (ask - bid) / price * 10000.0 if ask > bid > 0.0 and price > 0.0 else 99.0
    atr = _number(getattr(snapshot, 'atr_1m', 0.0))
    atr_bps = atr / price * 10000.0 if price > 0.0 else 0.0
    penalty = (
        5.0 * (1.0 - _clamp(data_confidence))
        + 4.0 * _sigmoid((spread_bps - 3.0) / 0.75)
        + 2.0 * _sigmoid((2.0 - atr_bps) / 0.50)
        + 2.0 * _sigmoid((atr_bps - 40.0) / 5.0)
    )
    return _clamp(base + penalty, 28.0, 45.0), spread_bps, atr_bps


def _target_notional_pct(trade_power, floor):
    margin = float(trade_power) - float(floor)
    if margin < 0.0:
        return 0.0
    progress = _clamp(margin / max(1.0, 100.0 - float(floor)))
    return MIN_TARGET_NOTIONAL_PCT + (
        MAX_TARGET_NOTIONAL_PCT - MIN_TARGET_NOTIONAL_PCT
    ) * progress ** 1.20


def _persistence_required_ms(trade_power, floor, confidence, activation):
    margin_quality = _clamp((float(trade_power) - float(floor)) / 35.0)
    readiness = _clamp(0.65 * margin_quality + 0.20 * confidence + 0.15 * activation)
    return 1200.0 * (1.0 - readiness) ** 1.20


def _tier(score):
    if score < 55.0:
        return 'WATCH'
    if score < 65.0:
        return 'MICRO'
    if score < 75.0:
        return 'NORMAL'
    if score < 85.0:
        return 'STRONG'
    return 'HIGH'


def _score_side(
    side, components, snapshot, setup, momentum, microflow_timing,
    mode_family, price, include_poc_location=True,
):
    weights = _weights(mode_family)
    location_modifier, location_metrics = _location_modifier(
        snapshot, setup, side, price, include_poc=include_poc_location
    )
    retest_fit, distance_atr, holding_probability = _retest_fit(
        snapshot, setup, side, momentum, price
    )
    raw = 0.0
    support_total = 0.0
    opposition_total = 0.0
    activation_remaining = 1.0
    activation_sources = []
    component_rows = []
    direct_abs = 0.0
    positive_direct = 0.0
    for component in components:
        effect, support, opposition, activation, triggers = _component_effect(
            component, side, weights
        )
        adjusted = effect * location_modifier if effect > 0.0 else effect
        raw += adjusted
        direct_abs += abs(adjusted)
        positive_direct += max(0.0, adjusted)
        support_total += support
        opposition_total += opposition
        if activation > 0.0:
            activation_remaining *= 1.0 - _clamp(activation * location_modifier)
            activation_sources.extend(triggers)
        component_rows.append({
            'component_id': component['component_id'],
            'members': [item['name'] for item in component['members']],
            'dependencies': component['dependencies'],
            'effect': round(adjusted, 6),
            'support': round(support, 6), 'opposition': round(opposition, 6),
            'event_time_min': min(
                (_number(item.get('ts')) for item in component['members']),
                default=0.0,
            ),
            'event_time_max': max(
                (_number(item.get('ts')) for item in component['members']),
                default=0.0,
            ),
        })

    interaction_candidates = []
    for left_index, left in enumerate(component_rows):
        if left['effect'] <= 0.0:
            continue
        for right in component_rows[left_index + 1:]:
            if right['effect'] <= 0.0:
                continue
            if set(left['dependencies']) & set(right['dependencies']):
                continue
            strength = 0.15 * min(left['effect'], right['effect'])
            interaction_candidates.append({
                'left': left['component_id'], 'right': right['component_id'],
                'effect': strength,
            })
    interaction_cap = 0.25 * positive_direct
    interaction_total = min(
        interaction_cap,
        sum(item['effect'] for item in interaction_candidates),
    )
    raw += interaction_total
    remaining_interaction = interaction_total
    interactions = []
    for item in interaction_candidates:
        applied = min(item['effect'], remaining_interaction)
        if applied <= 0.0:
            break
        interactions.append({**item, 'effect': round(applied, 6)})
        remaining_interaction -= applied

    aligned_momentum = _side_sign(side) * momentum['state']
    momentum_effect = 1.10 * aligned_momentum
    raw += momentum_effect
    if aligned_momentum > 0.0:
        support_total += abs(momentum_effect) * momentum['coverage']
    elif aligned_momentum < 0.0:
        opposition_total += abs(momentum_effect) * momentum['coverage']
    opposing_momentum = max(0.0, -aligned_momentum)
    impulse_conflict = _smoothstep(0.20, 0.80, opposing_momentum)
    raw -= 1.50 * impulse_conflict
    adverse_memory = _recent_adverse_flow(
        snapshot, side, _number(getattr(snapshot, 'snapshot_time', 0.0))
    )
    adverse_memory_strength = adverse_memory['active_strength']
    adverse_memory_effect = 1.25 * adverse_memory_strength
    raw -= adverse_memory_effect

    neutral_lane = mode_family == 'NEUTRAL_MOMENTUM'
    if neutral_lane:
        acceptance_side = (
            momentum['acceptance_long'] if side == 'LONG'
            else momentum['acceptance_short']
        )
        momentum_activation = _clamp(
            max(0.0, aligned_momentum) * (0.35 + 0.65 * acceptance_side)
        )
        activation_remaining *= 1.0 - momentum_activation
        if momentum_activation > 0.0:
            activation_sources.append('NEUTRAL_ACCEPTANCE_MOMENTUM')

    activation = _clamp(1.0 - activation_remaining)
    activation *= 1.0 - 0.80 * impulse_conflict
    activation *= 1.0 - 0.70 * adverse_memory_strength
    timing_aligned = max(0.0, _side_sign(side) * microflow_timing)
    activation *= 0.85 + 0.15 * timing_aligned
    if str(setup.get('kind', '')).lower() == 'breakout' or neutral_lane:
        activation *= retest_fit

    family_count = len([row for row in component_rows if abs(row['effect']) > 0.0])
    if momentum['coverage'] > 0.0 and abs(momentum['state']) > 0.01:
        family_count += 1
    causal_coverage = _clamp(1.0 - math.exp(-family_count / 2.0))
    if neutral_lane and abs(momentum['state']) > 0.01:
        # The momentum component already contains three causal horizons and
        # two source families; do not mislabel it as a single sparse event.
        causal_coverage = max(
            causal_coverage, 0.55 + 0.25 * momentum['coverage']
        )
    support_share = support_total / max(support_total + opposition_total, 1e-9)
    confidence = _clamp(
        causal_coverage * momentum['coverage']
        * (0.40 + 0.60 * support_share)
    )
    confidence *= 1.0 - 0.35 * adverse_memory_strength
    score = _clamp(50.0 + 50.0 * math.tanh(raw / 3.0), 0.0, 100.0)
    trade_power = score * confidence * activation
    return {
        'side': side, 'raw': round(raw, 6), 'score': round(score, 4),
        'confidence': round(confidence, 4), 'activation': round(activation, 4),
        'trade_power': round(trade_power, 4),
        'activation_sources': list(dict.fromkeys(activation_sources)),
        'causal_components': component_rows,
        'interactions': interactions,
        'location_modifier': round(location_modifier, 6),
        'location_metrics': location_metrics,
        'momentum_effect': round(momentum_effect, 6),
        'microflow_timing': round(microflow_timing, 6),
        'opposing_momentum': round(opposing_momentum, 6),
        'impulse_conflict': round(impulse_conflict, 6),
        'recent_adverse_flow_memory': {
            **adverse_memory,
            'active_strength': round(adverse_memory_strength, 6),
            'age_seconds': (
                round(adverse_memory['age_seconds'], 6)
                if adverse_memory['age_seconds'] is not None else None
            ),
        },
        'adverse_flow_memory_effect': round(adverse_memory_effect, 6),
        'retest_fit': round(retest_fit, 6),
        'retest_distance_atr': round(distance_atr, 6),
        'holding_side_probability': round(holding_probability, 6),
        'direct_score_abs': round(direct_abs, 6),
    }


def score_continuous(
    snapshot, setup, mode_info=None, live=True, include_poc_location=True,
):
    """Score both directions from one immutable snapshot without side effects."""
    now = _number(getattr(snapshot, 'snapshot_time', 0.0))
    if now <= 0.0:
        raise ValueError('continuous V2 requires snapshot_time')
    mode = str(setup.get('mode') or (mode_info or {}).get('mode') or '')
    mode_family = _mode_family(mode)
    bid = _number(getattr(snapshot, 'best_bid', 0.0))
    ask = _number(getattr(snapshot, 'best_ask', 0.0))
    price = (bid + ask) / 2.0 if ask > bid > 0.0 else max(bid, ask)
    events = (
        ('M15_STRUCTURE', 'structure', 'continuous_m15', 3600.0, False),
        ('SWEEP_M1', 'reaction', 'continuous_sweep_m1', 120.0, True),
        ('BREAKOUT_M1', 'structure', 'continuous_breakout_m1', 15.0, True),
        ('FOOTPRINT', 'flow', 'continuous_footprint', 15.0, True),
        ('PERSISTENT_FLOW', 'flow', 'continuous_persistent_flow', 5.0, True),
        ('ZONE_REACTION', 'reaction', 'continuous_zone_reaction', 15.0, True),
        ('FLOW_DIVERGENCE', 'reaction', 'continuous_flow_divergence', 2.0, True),
        ('ABSORPTION_REACTION', 'reaction', 'continuous_absorption_reaction', 5.0, True),
        ('VALUE_AREA_SWEEP', 'reaction', 'continuous_value_area_sweep', 20.0, True),
    )
    items = [
        _continuous_item(name, group, _event(snapshot, field), now, ttl, activation)
        for name, group, field, ttl, activation in events
    ]
    components = _causal_components(items)
    momentum = _normalized_momentum(snapshot)
    buy3 = max(0.0, _number(getattr(snapshot, 'current_cvd_buy_3s', 0.0)))
    sell3 = max(0.0, _number(getattr(snapshot, 'current_cvd_sell_3s', 0.0)))
    total3 = buy3 + sell3
    p90 = max(_number(getattr(snapshot, 'vol_pct90', 0.0)), 1e-9)
    microflow_timing = (
        _clamp((buy3 - sell3) / total3, -1.0, 1.0)
        * _clamp(total3 / max(0.5 * p90, 1e-9))
        if total3 > 0.0 else 0.0
    )
    sides = {
        side: _score_side(
            side, components, snapshot, setup, momentum, microflow_timing,
            mode_family, price, include_poc_location=include_poc_location,
        ) for side in ('LONG', 'SHORT')
    }
    selected_bias = str(setup.get('bias') or '')
    if selected_bias not in sides:
        selected_bias = max(sides, key=lambda value: sides[value]['trade_power'])
    selected = sides[selected_bias]
    fresh_items = [item for item in items if item['freshness'] > 0.0 and item['strength'] > 0.0]
    evidence_confidence = (
        sum(item['quality'] * item['freshness'] for item in fresh_items) / len(fresh_items)
        if fresh_items else 0.0
    )
    data_confidence = _clamp(0.55 * evidence_confidence + 0.45 * momentum['coverage'])
    floor, spread_bps, atr_bps = _activation_floor(
        snapshot, mode_family, data_confidence, price
    )
    size_pct = _target_notional_pct(selected['trade_power'], floor)
    size_pct *= 1.0 - 0.85 * selected['impulse_conflict']
    if mode_family in ('BREAKOUT', 'NEUTRAL_MOMENTUM'):
        size_pct *= selected['retest_fit']
    neutral_lane = mode_family == 'NEUTRAL_MOMENTUM'
    if neutral_lane:
        size_pct = min(size_pct, NEUTRAL_MOMENTUM_MAX_PCT)
    if selected['trade_power'] >= floor and 0.0 < size_pct < MIN_TARGET_NOTIONAL_PCT:
        size_pct = MIN_TARGET_NOTIONAL_PCT
    quality_flags = []
    if momentum['coverage'] < 0.60:
        quality_flags.append('MOMENTUM_HISTORY_WARMUP')
    if data_confidence < 0.45:
        quality_flags.append('LOW_DATA_CONFIDENCE')
    if spread_bps > 2.0:
        quality_flags.append('WIDE_SPREAD')
    if any(_number(_event(snapshot, field).get('ts')) > now + 0.25 for _, _, field, _, _ in events):
        quality_flags.append('FUTURE_TIMESTAMP_REJECTED')
    source_ids = list(dict.fromkeys(
        item['source_event_id'] for item in items if item.get('source_event_id')
    ))
    return {
        'version': LIVE_VERSION, 'snapshot_time': now,
        'opportunity_id': setup.get('opportunity_id') or setup.get('semantic_key'),
        'setup_id': setup.get('setup_id'),
        'setup_generation': int(setup.get('generation', 0) or 0),
        'mode': mode, 'mode_family': mode_family,
        'selected_bias': selected_bias,
        'score': selected['score'], 'confidence': selected['confidence'],
        'activation': selected['activation'], 'trade_power': selected['trade_power'],
        'activation_floor': round(floor, 4),
        'activated': bool(selected['trade_power'] >= floor),
        'display_tier': _tier(selected['score']),
        'target_notional_pct': round(size_pct, 4),
        'allocation_unit': 'TARGET_NOTIONAL_PCT_OF_EQUITY',
        'power_margin': round(selected['trade_power'] - floor, 4),
        'data_confidence': round(data_confidence, 4),
        'spread_bps': round(spread_bps, 4), 'atr_bps': round(atr_bps, 4),
        'momentum_state': round(momentum['state'], 6),
        'momentum_breakdown': momentum,
        'causal_components': selected['causal_components'],
        'location_modifier': selected['location_modifier'],
        'impulse_conflict': selected['impulse_conflict'],
        'recent_adverse_flow_memory': selected['recent_adverse_flow_memory'],
        'adverse_flow_memory_effect': selected['adverse_flow_memory_effect'],
        'retest_fit': selected['retest_fit'],
        'entry_style_policy': 'PASSIVE_RETEST' if neutral_lane else None,
        'value_migration_retest': bool(
            setup.get('value_migration_retest', False)
        ),
        'value_boundary': setup.get('value_boundary'),
        'passive_intent_ttl_seconds': round(
            30.0 + 60.0 * selected['confidence'] * selected['activation'], 4
        ),
        'neutral_momentum_lane': neutral_lane,
        'sides': sides, 'source_event_ids': source_ids,
        'evidence_quality_flags': quality_flags, 'live_authority': bool(live),
        'poc_location_included': bool(include_poc_location),
    }


def entry_ready(setup, score, now_mono):
    if score.get('version') != LIVE_VERSION:
        return False, 'CONTINUOUS_V2_LIVE_VERSION_REQUIRED'
    fields = ('score', 'confidence', 'activation', 'trade_power', 'activation_floor', 'target_notional_pct')
    numeric = all(math.isfinite(_number(score.get(field))) for field in fields)
    units_valid = bool(
        0.0 <= _number(score.get('score')) <= 100.0
        and 0.0 <= _number(score.get('confidence')) <= 1.0
        and 0.0 <= _number(score.get('activation')) <= 1.0
        and 0.0 <= _number(score.get('trade_power')) <= 100.0
        and 0.0 <= _number(score.get('target_notional_pct')) <= MAX_TARGET_NOTIONAL_PCT
        and score.get('allocation_unit') == 'TARGET_NOTIONAL_PCT_OF_EQUITY'
    )
    eligible = bool(
        numeric and units_valid and score.get('activated')
        and score.get('selected_bias') == setup.get('bias')
        and _number(score.get('target_notional_pct')) > 0.0
        and _number(score.get('impulse_conflict')) <= 0.75
    )
    if not eligible:
        setup.pop('continuous_eligible_since_mono', None)
        if numeric and not units_valid:
            return False, 'CONTINUOUS_V2_UNIT_CONTRACT_INVALID'
        return False, 'CONTINUOUS_V2_POWER_OR_DIRECTION_BELOW_ENTRY'
    eligible_since = setup.setdefault('continuous_eligible_since_mono', float(now_mono))
    required_ms = _persistence_required_ms(
        score.get('trade_power'), score.get('activation_floor'),
        score.get('confidence'), score.get('activation'),
    )
    elapsed_ms = max(0.0, float(now_mono) - float(eligible_since)) * 1000.0
    if elapsed_ms + 1e-6 < required_ms:
        return False, 'CONTINUOUS_V2_PERSISTENCE_WAIT_%dMS' % math.ceil(required_ms - elapsed_ms)
    return True, 'CONTINUOUS_V2_PERSISTENCE_PASS'
