"""CONTINUOUS_SHADOW_V1: scorer pure, không có quyền claim hay sửa live state."""

import math


VERSION = 'CONTINUOUS_SHADOW_V1'
LIVE_VERSION = 'CONTINUOUS_V1'
MIN_TARGET_NOTIONAL_PCT = 0.30
MAX_TARGET_NOTIONAL_PCT = 9.0


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def _number(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def _sigmoid(value):
    """Stable continuous transition used by adaptive safety penalties."""
    value = float(value)
    if value >= 0.0:
        decay = math.exp(-value)
        return 1.0 / (1.0 + decay)
    growth = math.exp(value)
    return growth / (1.0 + growth)


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
    if 'BREAKOUT' in mode:
        return 'BREAKOUT'
    if 'PULLBACK' in mode:
        return 'TREND_PULLBACK'
    if 'FADE' in mode or 'NEUTRAL' in mode:
        return 'LIQUIDITY_REVERSAL'
    return 'BALANCED'


def _importance(mode_family):
    return {
        'TREND_PULLBACK': {
            'structure': 1.20, 'location': 1.10, 'reaction': 1.00,
            'flow': 0.75, 'liquidity': 0.45,
        },
        'BREAKOUT': {
            'structure': 0.90, 'location': 0.60, 'reaction': 0.90,
            'flow': 1.20, 'liquidity': 0.70,
        },
        'LIQUIDITY_REVERSAL': {
            'structure': 0.50, 'location': 1.20, 'reaction': 1.25,
            'flow': 0.90, 'liquidity': 0.70,
        },
        'BALANCED': {
            'structure': 0.85, 'location': 0.90, 'reaction': 1.00,
            'flow': 0.90, 'liquidity': 0.65,
        },
    }[mode_family]


def _continuous_item(name, group, event, now, ttl, activation=False):
    direction = _clamp(_number(event.get('direction')), -1.0, 1.0)
    strength = _clamp(_number(event.get('strength')))
    quality = _clamp(_number(event.get('quality'), 0.5 if strength else 0.0))
    freshness = _freshness(event, now, ttl)
    dependencies = list(event.get('dependency_families', ()) or ())
    family = event.get('source_family')
    if family and family not in dependencies:
        dependencies.append(str(family))
    return {
        'name': name,
        'group': group,
        'direction': direction,
        'strength': strength,
        'quality': quality,
        'freshness': freshness,
        'activation_source': bool(activation),
        'source_event_id': event.get('source_event_id') or event.get('event_id'),
        'parent_event_id': event.get('parent_event_id') or event.get('source_event_id'),
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


def _derived_items(snapshot, setup, now):
    atr = max(_number(getattr(snapshot, 'atr_1m', 0.0)), 1e-9)
    bid = _number(getattr(snapshot, 'best_bid', 0.0))
    ask = _number(getattr(snapshot, 'best_ask', 0.0))
    price = (bid + ask) / 2.0 if ask > bid > 0.0 else max(bid, ask)
    setup_bias = str(setup.get('bias') or '')
    setup_direction = _side_sign(setup_bias) if setup_bias in ('LONG', 'SHORT') else 0.0
    zone = _number(setup.get('zone'))
    proximity = _clamp(1.0 - abs(price - zone) / (1.25 * atr)) if zone > 0.0 else 0.0
    items = [{
        'name': 'FROZEN_ZONE_LOCATION', 'group': 'location',
        'direction': setup_direction, 'strength': proximity,
        'quality': 1.0, 'freshness': 1.0, 'activation_source': False,
        'source_event_id': f"zone:{setup.get('zone_id')}:{zone:.4f}",
        'parent_event_id': None, 'dependencies': ['SETUP_LOCATION'],
        'metrics': {'distance_atr': abs(price - zone) / atr if zone > 0.0 else None},
    }]

    buy = _number(getattr(snapshot, 'current_cvd_buy_3s', 0.0))
    sell = _number(getattr(snapshot, 'current_cvd_sell_3s', 0.0))
    total = buy + sell
    p90 = _number(getattr(snapshot, 'vol_pct90', 0.0))
    if total > 0.0:
        imbalance = (buy - sell) / total
        materiality = _clamp(total / max(0.5 * p90, 1e-9)) if p90 > 0.0 else 0.0
        items.append({
            'name': 'MICROFLOW_3S', 'group': 'flow',
            'direction': 1.0 if imbalance > 0.0 else -1.0 if imbalance < 0.0 else 0.0,
            'strength': _clamp(abs(imbalance) / 0.70) * materiality,
            'quality': materiality, 'freshness': 1.0,
            'activation_source': True,
            'source_event_id': f"microflow:{int(now // 3)}",
            'parent_event_id': None, 'dependencies': ['AGGTRADE'],
            'metrics': {
                'buy': buy, 'sell': sell, 'total': total,
                'imbalance': imbalance, 'materiality': materiality,
            },
        })

    obi = (
        0.45 * _number(getattr(snapshot, 'obi_top3', 0.0))
        + 0.35 * _number(getattr(snapshot, 'obi_top10', 0.0))
        + 0.20 * _number(getattr(snapshot, 'obi', 0.0))
    )
    if abs(obi) > 0.01:
        obi_direction = 1.0 if obi > 0.0 else -1.0
        progress = _number(getattr(snapshot, 'price_progress_atr_3s', 0.0))
        aligned = progress * obi_direction
        classification = 'OBI_UNCONFIRMED'
        relation = 0.30
        output_direction = obi_direction
        if aligned >= 0.02:
            relation = _clamp(aligned / 0.12)
            classification = 'OBI_PRICE_CONFIRMED'
        elif aligned <= -0.02:
            relation = _clamp(abs(aligned) / 0.12)
            output_direction = -obi_direction
            classification = 'OBI_TRAPPED_OR_ABSORBED'
        history = list(getattr(snapshot, 'obi_history', ()) or ())[-10:]
        same_sign = sum(
            1 for item in history
            if isinstance(item, (list, tuple)) and len(item) >= 2
            and _number(item[1]) * obi_direction > 0.0
        )
        persistence = same_sign / len(history) if history else 0.0
        items.append({
            'name': classification, 'group': 'liquidity',
            'direction': output_direction,
            'strength': _clamp(abs(obi) / 0.75) * relation,
            'quality': _clamp(0.35 + 0.65 * persistence),
            'freshness': 1.0, 'activation_source': False,
            'source_event_id': f"obi:{int(now * 5)}",
            'parent_event_id': None, 'dependencies': ['DEPTH'],
            'metrics': {
                'obi': obi, 'price_progress_atr_3s': progress,
                'persistence': persistence,
            },
        })

    poc = _number(getattr(snapshot, 'poc', 0.0))
    if price > 0.0 and poc > 0.0:
        delta = (poc - price) / atr
        items.append({
            'name': 'POC_ATTRACTION', 'group': 'liquidity',
            'direction': 1.0 if delta > 0.0 else -1.0,
            'strength': _clamp(abs(delta) / 1.5) * _clamp(1.0 - abs(delta) / 4.0),
            'quality': 0.65, 'freshness': 1.0,
            'activation_source': False,
            'source_event_id': f"poc:{poc:.4f}", 'parent_event_id': None,
            'dependencies': ['VALUE_AREA'], 'metrics': {'distance_atr': delta},
        })
    return items, price


def _apply_independence(items):
    counts = {}
    parent_counts = {}
    for item in items:
        if item['strength'] <= 0.0 or item['freshness'] <= 0.0:
            continue
        for dependency in set(item['dependencies']):
            counts[dependency] = counts.get(dependency, 0) + 1
        parent = item.get('parent_event_id')
        if parent:
            parent_counts[parent] = parent_counts.get(parent, 0) + 1
    for item in items:
        largest = max((counts.get(dep, 1) for dep in item['dependencies']), default=1)
        independence = max(0.35, 1.0 / math.sqrt(max(1, largest)))
        if item.get('parent_event_id') and parent_counts.get(item['parent_event_id'], 0) > 1:
            independence *= 0.70
        item['independence'] = _clamp(independence)


def _interaction(name, direction, strength, dependencies, sources):
    return {
        'name': name, 'direction': direction, 'strength': _clamp(strength),
        'dependencies': list(dependencies), 'source_event_ids': list(sources),
    }


def _find(items, name):
    return next((item for item in items if item['name'] == name), None)


def _build_interactions(items, mode_family, setup):
    by_name = {item['name']: item for item in items}
    sweep = by_name.get('SWEEP_M1') or by_name.get('VALUE_AREA_SWEEP')
    zone = by_name.get('ZONE_REACTION')
    footprint = by_name.get('FOOTPRINT')
    flow = by_name.get('PERSISTENT_FLOW') or by_name.get('MICROFLOW_3S')
    breakout = by_name.get('BREAKOUT_M1')
    absorption = by_name.get('ABSORPTION_REACTION')
    interactions = []

    def aligned(a, b):
        return a and b and a['direction'] * b['direction'] > 0.0

    if sweep:
        precision = _number(sweep['metrics'].get('zone_precision'), 0.5)
        reclaim = _clamp(_number(sweep['metrics'].get('reclaim_distance_atr')) / 0.12)
        interactions.append(_interaction(
            'SWEEP_RECLAIM_X_LOCATION', sweep['direction'],
            sweep['strength'] * sweep['quality'] * precision * reclaim,
            ['PRICE_REACTION', 'SETUP_LOCATION'], [sweep.get('source_event_id')],
        ))
    if aligned(zone, flow):
        interactions.append(_interaction(
            'ZONE_REACTION_X_FLOW', zone['direction'],
            zone['strength'] * flow['strength'] * zone['independence'] * flow['independence'],
            set(zone['dependencies'] + flow['dependencies']),
            [zone.get('source_event_id'), flow.get('source_event_id')],
        ))
    if aligned(footprint, zone or sweep):
        reaction = zone or sweep
        interactions.append(_interaction(
            'FOOTPRINT_X_PRICE_REACTION', footprint['direction'],
            footprint['strength'] * reaction['strength']
            * footprint['independence'] * reaction['independence'],
            set(footprint['dependencies'] + reaction['dependencies']),
            [footprint.get('source_event_id'), reaction.get('source_event_id')],
        ))
    if mode_family == 'BREAKOUT' and aligned(breakout, flow):
        retest_fit = 1.0 if str(setup.get('activation_reason', '')).upper() in (
            'RETEST_CONFIRMED', 'LEVEL_CONFIRMED', 'RETEST_HELD',
        ) else 0.25
        interactions.append(_interaction(
            'BREAKOUT_X_FLOW_X_RETEST', breakout['direction'],
            breakout['strength'] * flow['strength'] * retest_fit,
            set(breakout['dependencies'] + flow['dependencies']),
            [breakout.get('source_event_id'), flow.get('source_event_id')],
        ))
    if absorption and flow and absorption['direction'] * flow['direction'] < 0.0:
        interactions.append(_interaction(
            'ABSORPTION_OF_AGGRESSIVE_FLOW', absorption['direction'],
            absorption['strength'] * flow['strength']
            * absorption['independence'] * flow['independence'],
            set(absorption['dependencies'] + flow['dependencies']),
            [absorption.get('source_event_id'), flow.get('source_event_id')],
        ))
    return interactions


def _score_side(side, items, interactions, importance, snapshot, setup):
    sign = _side_sign(side)
    effects = []
    raw = 0.0
    support_quality = 0.0
    total_quality = 0.0
    activation_remaining = 1.0
    activation_sources = []
    for item in items:
        relevance = importance.get(item['group'], 0.75)
        magnitude = (
            item['strength'] * item['quality'] * item['freshness']
            * item['independence'] * relevance
        )
        signed = item['direction'] * sign
        effect = signed * magnitude
        raw += effect
        total_quality += magnitude
        if signed > 0.0:
            support_quality += magnitude
            if item['activation_source']:
                activation_remaining *= 1.0 - _clamp(magnitude)
                if magnitude > 0.0:
                    activation_sources.append(item['name'])
        effects.append({
            'name': item['name'], 'group': item['group'],
            'effect': round(effect, 6), 'strength': round(item['strength'], 6),
            'quality': round(item['quality'], 6),
            'freshness': round(item['freshness'], 6),
            'independence': round(item['independence'], 6),
            'relevance': round(relevance, 6),
            'source_event_id': item.get('source_event_id'),
            'dependencies': list(item['dependencies']),
        })

    interaction_effects = []
    for item in interactions:
        effect = item['direction'] * sign * item['strength'] * 0.75
        raw += effect
        interaction_effects.append({**item, 'effect': round(effect, 6)})

    conflicts = []
    trap = _event(snapshot, 'flow_price_trap')
    if trap.get('active') and trap.get('blocked_bias') == side:
        strength = _clamp(abs(_number(trap.get('imbalance_15s'))) / 0.60)
        raw -= 0.90 * strength
        conflicts.append({'name': 'FLOW_WITHOUT_PRICE_PROGRESS', 'strength': strength})
    acceptance = _event(snapshot, 'zone_acceptance_trap')
    if acceptance.get('active') and acceptance.get('blocked_bias') == side:
        strength = _clamp(abs(_number(acceptance.get('adverse_atr'))) / 0.35)
        raw -= 1.10 * strength
        conflicts.append({'name': 'ZONE_ACCEPTANCE', 'strength': strength})
    if _mode_family(setup.get('mode')) == 'BREAKOUT' and str(
        setup.get('activation_reason', '')
    ).upper() not in ('RETEST_CONFIRMED', 'LEVEL_CONFIRMED', 'RETEST_HELD'):
        breakout = _find(items, 'BREAKOUT_M1')
        strength = 0.35 * (breakout['strength'] if breakout else 0.0)
        raw -= strength
        conflicts.append({'name': 'BREAKOUT_CHASE', 'strength': strength})

    score = _clamp(50.0 + 50.0 * math.tanh(raw / 3.0), 0.0, 100.0)
    activation = _clamp(1.0 - activation_remaining)
    support_share = support_quality / total_quality if total_quality > 0.0 else 0.0
    coverage = _clamp(total_quality / 2.5)
    confidence = _clamp(coverage * (0.35 + 0.65 * support_share))
    return {
        'side': side, 'raw': round(raw, 6), 'score': round(score, 4),
        'confidence': round(confidence, 4),
        'activation': round(activation, 4),
        'trade_power': round(score * confidence * activation, 4),
        'activation_sources': list(dict.fromkeys(activation_sources)),
        'evidence_effects': effects, 'interactions': interaction_effects,
        'conflicts': conflicts,
    }


def _activation_floor(snapshot, mode_family, data_confidence, price):
    base = {
        'TREND_PULLBACK': 30.0, 'BREAKOUT': 35.0,
        'LIQUIDITY_REVERSAL': 33.0, 'BALANCED': 34.0,
    }[mode_family]
    bid = _number(getattr(snapshot, 'best_bid', 0.0))
    ask = _number(getattr(snapshot, 'best_ask', 0.0))
    spread_bps = (ask - bid) / price * 10000.0 if ask > bid > 0.0 and price > 0.0 else 99.0
    atr = _number(getattr(snapshot, 'atr_1m', 0.0))
    atr_bps = atr / price * 10000.0 if price > 0.0 else 0.0
    uncertainty_penalty = 5.0 * (1.0 - _clamp(data_confidence))
    # No threshold cliff: every extra fraction of a basis point changes the
    # floor smoothly. The logistic caps prevent a broken quote from producing
    # an unbounded score requirement.
    spread_penalty = 4.0 * _sigmoid((spread_bps - 3.0) / 0.75)
    low_vol_penalty = 2.0 * _sigmoid((2.0 - atr_bps) / 0.50)
    high_vol_penalty = 2.0 * _sigmoid((atr_bps - 40.0) / 5.0)
    penalty = (
        uncertainty_penalty + spread_penalty
        + low_vol_penalty + high_vol_penalty
    )
    return _clamp(base + penalty, 28.0, 45.0), spread_bps, atr_bps


def _target_notional_pct(trade_power, floor):
    """Map positive power margin continuously to notional/equity allocation."""
    margin = float(trade_power) - float(floor)
    if margin < 0.0:
        return 0.0
    progress = _clamp(margin / max(1.0, 100.0 - float(floor)))
    return MIN_TARGET_NOTIONAL_PCT + (
        MAX_TARGET_NOTIONAL_PCT - MIN_TARGET_NOTIONAL_PCT
    ) * progress ** 1.20


def _persistence_required_ms(trade_power, floor, confidence, activation):
    """Strong margins claim quickly; marginal evidence must persist longer."""
    margin_quality = _clamp((float(trade_power) - float(floor)) / 35.0)
    readiness = _clamp(
        0.65 * margin_quality
        + 0.20 * _clamp(confidence)
        + 0.15 * _clamp(activation)
    )
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


def score_continuous(snapshot, setup, mode_info=None, live=False):
    """Chấm hai phía từ một immutable snapshot; không mutate input/state."""
    now = _number(getattr(snapshot, 'snapshot_time', 0.0))
    if now <= 0.0:
        raise ValueError('continuous scorer requires snapshot_time')
    mode = str(setup.get('mode') or (mode_info or {}).get('mode') or '')
    mode_family = _mode_family(mode)
    importance = _importance(mode_family)
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
    derived, price = _derived_items(snapshot, setup, now)
    items.extend(derived)
    _apply_independence(items)
    interactions = _build_interactions(items, mode_family, setup)
    sides = {
        side: _score_side(side, items, interactions, importance, snapshot, setup)
        for side in ('LONG', 'SHORT')
    }
    selected_bias = str(setup.get('bias') or '')
    if selected_bias not in sides:
        selected_bias = max(sides, key=lambda side: sides[side]['trade_power'])
    selected = sides[selected_bias]
    fresh_items = [item for item in items if item['freshness'] > 0.0 and item['strength'] > 0.0]
    data_confidence = (
        sum(item['quality'] * item['freshness'] for item in fresh_items) / len(fresh_items)
        if fresh_items else 0.0
    )
    floor, spread_bps, atr_bps = _activation_floor(
        snapshot, mode_family, data_confidence, price
    )
    target_notional_pct = _target_notional_pct(selected['trade_power'], floor)
    quality_flags = []
    if not fresh_items:
        quality_flags.append('NO_FRESH_CONTINUOUS_EVIDENCE')
    if data_confidence < 0.45:
        quality_flags.append('LOW_DATA_CONFIDENCE')
    if spread_bps > 2.0:
        quality_flags.append('WIDE_SPREAD')
    if any(
        _number((_event(snapshot, field)).get('ts')) > now + 0.25
        for _, _, field, _, _ in events
    ):
        quality_flags.append('FUTURE_TIMESTAMP_REJECTED')
    source_ids = list(dict.fromkeys(
        item['source_event_id'] for item in items if item.get('source_event_id')
    ))
    return {
        'version': LIVE_VERSION if live else VERSION,
        'snapshot_time': now,
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
        'target_notional_pct': round(target_notional_pct, 4),
        'allocation_unit': 'TARGET_NOTIONAL_PCT_OF_EQUITY',
        'power_margin': round(selected['trade_power'] - floor, 4),
        'data_confidence': round(data_confidence, 4),
        'spread_bps': round(spread_bps, 4), 'atr_bps': round(atr_bps, 4),
        'context_importance': importance,
        'sides': sides,
        'source_event_ids': source_ids,
        'evidence_quality_flags': quality_flags,
        'live_authority': bool(live),
    }


def entry_ready(setup, score, now_mono):
    """Continuous live persistence; no Boolean fast/normal track split."""
    if score.get('version') != LIVE_VERSION:
        return False, 'CONTINUOUS_LIVE_VERSION_REQUIRED'
    numeric = all(math.isfinite(_number(score.get(field))) for field in (
        'score', 'confidence', 'activation', 'trade_power', 'activation_floor',
        'target_notional_pct',
    ))
    units_valid = bool(
        0.0 <= _number(score.get('score')) <= 100.0
        and 0.0 <= _number(score.get('confidence')) <= 1.0
        and 0.0 <= _number(score.get('activation')) <= 1.0
        and 0.0 <= _number(score.get('trade_power')) <= 100.0
        and 0.0 <= _number(score.get('activation_floor')) <= 100.0
        and 0.0 <= _number(score.get('target_notional_pct')) <= MAX_TARGET_NOTIONAL_PCT
        and score.get('allocation_unit') == 'TARGET_NOTIONAL_PCT_OF_EQUITY'
    )
    eligible = bool(
        numeric and units_valid and score.get('activated')
        and score.get('selected_bias') == setup.get('bias')
        and _number(score.get('target_notional_pct')) > 0.0
    )
    history = setup.setdefault('continuous_live_history', [])
    history.append({
        'ts': float(now_mono), 'eligible': eligible,
        'trade_power': _number(score.get('trade_power')),
        'source_ids': list(score.get('source_event_ids', ())),
    })
    del history[:-3]
    if not eligible:
        setup.pop('continuous_eligible_since_mono', None)
        if numeric and not units_valid:
            return False, 'CONTINUOUS_UNIT_CONTRACT_INVALID'
        return False, 'CONTINUOUS_POWER_OR_DIRECTION_BELOW_ENTRY'
    eligible_since = setup.setdefault(
        'continuous_eligible_since_mono', float(now_mono)
    )
    required_ms = _persistence_required_ms(
        score.get('trade_power'), score.get('activation_floor'),
        score.get('confidence'), score.get('activation'),
    )
    elapsed_ms = max(0.0, float(now_mono) - float(eligible_since)) * 1000.0
    if elapsed_ms + 1e-6 < required_ms:
        return False, 'CONTINUOUS_PERSISTENCE_WAIT_%dMS' % math.ceil(
            required_ms - elapsed_ms
        )
    return True, 'CONTINUOUS_PERSISTENCE_PASS'
