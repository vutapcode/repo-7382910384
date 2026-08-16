"""Pure Dynamic Path & Fee Gate V2.

The engine evaluates the exit path for an already selected side.  It never
chooses LONG/SHORT, mutates live state, or consumes future outcome fields.
"""

import math
import os


VERSION = 'DYNAMIC_PATH_V2'
MODEL_VERSION = 'PATH_PRIOR_META_BOOTSTRAP_V2'
FEATURE_SCHEMA = 'PATH_FEATURES_V2_CONTINUATION'
MARKET_EDGE_BPS = float(os.getenv('SMC_DYNAMIC_MARKET_EDGE_BPS', '4.0'))
REALIZABLE_MARKET_BUFFER_BPS = float(
    os.getenv('SMC_REALIZABLE_MARKET_BUFFER_BPS', '2.0')
)
TRAILING_RUNNER_RETENTION_LCB = float(
    os.getenv('SMC_TRAILING_RUNNER_RETENTION_LCB', '0.70')
)
MAX_TP1_ALLOCATION = 0.70
PASSIVE_SIZE_CAP_PCT = 2.0
CONTINUATION_MIN_SCORE = 72.0
CONTINUATION_MIN_CONFIDENCE = 0.68
CONTINUATION_MIN_ACTIVATION = 0.62
CONTINUATION_MIN_SUPPORT = 0.68
CONTINUATION_MIN_FLOW_RESPONSE = 0.62
CONTINUATION_MAX_IMPULSE_CONFLICT = 0.35
CONTINUATION_MIN_BASE_DISTANCE_BPS = 8.0


def _f(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _clamp(value, low=0.0, high=1.0):
    return max(float(low), min(float(high), _f(value)))


def _sigmoid(value):
    value = _f(value)
    if value >= 0.0:
        decay = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + decay)
    growth = math.exp(max(value, -60.0))
    return growth / (1.0 + growth)


def _side_sign(side):
    return 1.0 if side == 'LONG' else -1.0


def _favorable(side, entry, price, tick):
    price = _f(price)
    return bool(
        price > 0.0 and (
            price >= entry + 2.0 * tick
            if side == 'LONG' else price <= entry - 2.0 * tick
        )
    )


def _event(snapshot, name):
    value = getattr(snapshot, name, {}) or {}
    return value if isinstance(value, dict) else {}


def _event_direction(event):
    direction = event.get('direction')
    if isinstance(direction, str):
        return 1.0 if direction == 'LONG' else -1.0 if direction == 'SHORT' else 0.0
    return _clamp(direction, -1.0, 1.0)


def _source_levels(snapshot, signal, side):
    values = [
        ('POC', getattr(snapshot, 'poc', 0.0)),
        ('VAH' if side == 'LONG' else 'VAL', (
            getattr(snapshot, 'vah', 0.0)
            if side == 'LONG' else getattr(snapshot, 'val', 0.0)
        )),
        ('M15_SWING', (
            getattr(snapshot, 'swing_high_m15', 0.0)
            if side == 'LONG' else getattr(snapshot, 'swing_low_m15', 0.0)
        )),
        ('BREAKOUT_TARGET', signal.get('breakout_target', 0.0)),
        ('BREAKOUT_TARGET2', signal.get('breakout_target2', 0.0)),
    ]
    broken = _f(getattr(snapshot, 'structure_broken_level', 0.0))
    current = _f(
        getattr(snapshot, 'best_ask', 0.0)
        if side == 'LONG' else getattr(snapshot, 'best_bid', 0.0)
    )
    transition = str(getattr(snapshot, 'structure_transition', '') or '')
    streak = int(_f(getattr(snapshot, 'structure_break_streak', 0.0)))
    transition_supports = bool(
        (side == 'LONG' and transition == 'TRANSITION_BULLISH' and 0.0 < broken < current)
        or (side == 'SHORT' and transition == 'TRANSITION_BEARISH' and broken > current)
    )
    if transition_supports and streak >= 2:
        impulse = abs(current - broken)
        values.append((
            'STRUCTURE_MEASURED_MOVE',
            current + impulse if side == 'LONG' else current - impulse,
        ))
    extrema = list(getattr(snapshot, 'closed_m1_extrema', ()) or ())
    extrema.extend(list(getattr(snapshot, 'closed_m15_extrema', ()) or ()))
    for item in extrema:
        if not isinstance(item, dict):
            continue
        price = item.get('high') if side == 'LONG' else item.get('low')
        timeframe = str(item.get('timeframe') or 'M1')
        values.append((f'{timeframe}_EXTREMUM', price))
    return values


def build_path_candidates(snapshot, signal, entry_price, tick_size=0.1):
    """Build a deduped ladder using only levels known at evaluation time."""
    side = str(signal.get('bias') or '')
    entry = _f(entry_price)
    tick = max(_f(tick_size, 0.1), 1e-9)
    if side not in ('LONG', 'SHORT') or entry <= 0.0:
        return []
    merged = {}
    for source, raw_price in _source_levels(snapshot, signal, side):
        price = round(_f(raw_price) / tick) * tick
        if not _favorable(side, entry, price, tick):
            continue
        key = round(price / tick)
        record = merged.setdefault(key, {'price': price, 'sources': []})
        if source not in record['sources']:
            record['sources'].append(source)
    ordered = sorted(
        merged.values(), key=lambda item: item['price'],
        reverse=side == 'SHORT',
    )
    projection = _causal_continuation_projection(
        snapshot, signal, side, entry, ordered, tick,
    )
    if projection is not None:
        key = round(projection['price'] / tick)
        record = merged.setdefault(key, {
            'price': projection['price'], 'sources': [],
        })
        if 'CAUSAL_MEASURED_CONTINUATION' not in record['sources']:
            record['sources'].append('CAUSAL_MEASURED_CONTINUATION')
        record['continuation_meta'] = projection['continuation_meta']
        ordered = sorted(
            merged.values(), key=lambda item: item['price'],
            reverse=side == 'SHORT',
        )
    for index, item in enumerate(ordered):
        item['target_id'] = f"{side}:{item['price']:.8f}"
        item['rank'] = index + 1
    return ordered


def _causal_continuation_projection(
    snapshot, signal, side, entry, ordered, tick,
):
    """Add one measured continuation hypothesis from causal state only.

    This is a bounded bootstrap while ML Meta remains shadow. It does not
    authorize an entry: the ordinary reachability, fee and realizable-LCB
    gates still decide whether maker/market execution has positive edge.
    """
    if not ordered:
        return None
    mode = str(signal.get('mode') or '').upper()
    if not any(token in mode for token in ('PULLBACK', 'BREAKOUT')):
        return None
    trend = str(getattr(snapshot, 'trend_m15', '') or '').upper()
    if (side == 'LONG' and trend != 'BULLISH') or (
        side == 'SHORT' and trend != 'BEARISH'
    ):
        return None
    continuous = dict(signal.get('continuous_score', {}) or {})
    selected = dict((continuous.get('sides', {}) or {}).get(side, {}) or {})
    score = _f(selected.get('score'), _f(continuous.get('score')))
    confidence = _clamp(
        selected.get('confidence', continuous.get('confidence'))
    )
    activation = _clamp(
        selected.get('activation', continuous.get('activation'))
    )
    impulse_conflict = _clamp(
        selected.get('impulse_conflict', continuous.get('impulse_conflict'))
    )
    context = _score_context(snapshot, signal, side)
    if (
        score < CONTINUATION_MIN_SCORE
        or confidence < CONTINUATION_MIN_CONFIDENCE
        or activation < CONTINUATION_MIN_ACTIVATION
        or context['support'] < CONTINUATION_MIN_SUPPORT
        or context['flow_response'] < CONTINUATION_MIN_FLOW_RESPONSE
        or context['adverse'] > 0.15
        or impulse_conflict > CONTINUATION_MAX_IMPULSE_CONFLICT
    ):
        return None
    farthest = ordered[-1]
    base_distance = abs(_f(farthest['price']) - entry)
    base_distance_bps = base_distance / entry * 10000.0
    if base_distance_bps < CONTINUATION_MIN_BASE_DISTANCE_BPS:
        return None
    quality = _clamp(
        0.22 * _clamp((score - 55.0) / 35.0)
        + 0.18 * confidence + 0.18 * activation
        + 0.22 * context['support'] + 0.20 * context['flow_response']
    )
    # Continuous 0.50-0.85 measured extension. No ATR target and no time/chase
    # promotion; the leg is derived from the frozen entry-to-structure path.
    extension_ratio = 0.50 + 0.35 * quality
    direction = 1.0 if side == 'LONG' else -1.0
    projected = _f(farthest['price']) + direction * base_distance * extension_ratio
    projected = round(projected / tick) * tick
    if not _favorable(side, entry, projected, tick):
        return None
    return {
        'price': projected,
        'continuation_meta': {
            'policy_version': 'META_BOOTSTRAP_CAUSAL_PATH_V1',
            'anchor_price': _f(farthest['price']),
            'anchor_sources': list(farthest.get('sources', ())),
            'base_distance_bps': base_distance_bps,
            'extension_ratio': extension_ratio,
            'quality': quality,
            'score': score, 'confidence': confidence,
            'activation': activation, 'support': context['support'],
            'flow_response': context['flow_response'],
            'impulse_conflict': impulse_conflict,
            'future_fields_used': False,
            'ml_live_authority': False,
        },
    }


def _meta_bootstrap_audit(candidates):
    projected = next((
        item for item in candidates
        if 'CAUSAL_MEASURED_CONTINUATION' in item.get('sources', ())
    ), None)
    return {
        'policy_version': 'META_BOOTSTRAP_CAUSAL_PATH_V1',
        'activated': projected is not None,
        'live_authority': 'BOUNDED_RULE_BOOTSTRAP',
        'ml_authority': 'SHADOW_ONLY',
        'parameters': {
            'min_score': CONTINUATION_MIN_SCORE,
            'min_confidence': CONTINUATION_MIN_CONFIDENCE,
            'min_activation': CONTINUATION_MIN_ACTIVATION,
            'min_support': CONTINUATION_MIN_SUPPORT,
            'min_flow_response': CONTINUATION_MIN_FLOW_RESPONSE,
            'max_impulse_conflict': CONTINUATION_MAX_IMPULSE_CONFLICT,
            'min_base_distance_bps': CONTINUATION_MIN_BASE_DISTANCE_BPS,
            'extension_ratio_min': 0.50,
            'extension_ratio_max': 0.85,
        },
        'projected_target': _f(projected.get('price')) if projected else None,
        'projection': dict(projected.get('continuation_meta', {})) if projected else None,
    }


def _score_context(snapshot, signal, side):
    sign = _side_sign(side)
    continuous = dict(signal.get('continuous_score', {}) or {})
    selected = dict((continuous.get('sides', {}) or {}).get(side, {}) or {})
    score = _f(selected.get('score'), _f(continuous.get('score'), 50.0))
    confidence = _clamp(
        selected.get('confidence'), 0.0, 1.0
    ) if selected else _clamp(continuous.get('confidence'))
    activation = _clamp(
        selected.get('activation'), 0.0, 1.0
    ) if selected else _clamp(continuous.get('activation'))
    score_quality = _clamp((score - 40.0) / 50.0)

    buy = _f(getattr(snapshot, 'current_cvd_buy_3s', 0.0))
    sell = _f(getattr(snapshot, 'current_cvd_sell_3s', 0.0))
    total = buy + sell
    flow = ((buy - sell) / total) * sign if total > 0.0 else 0.0
    progress = _f(getattr(snapshot, 'price_progress_atr_3s', 0.0)) * sign
    obi = (
        0.45 * _f(getattr(snapshot, 'obi_top3', 0.0))
        + 0.35 * _f(getattr(snapshot, 'obi_top10', 0.0))
        + 0.20 * _f(getattr(snapshot, 'obi', 0.0))
    ) * sign
    flow_response = _clamp(
        0.45 * _clamp((flow + 1.0) / 2.0)
        + 0.35 * _clamp((progress + 0.15) / 0.30)
        + 0.20 * _clamp((obi + 0.75) / 1.50)
    )

    trap = _event(snapshot, 'flow_price_trap')
    acceptance = _event(snapshot, 'zone_acceptance_trap')
    adverse = 0.0
    if trap.get('active') and trap.get('blocked_bias') == side:
        adverse += 0.60
    if acceptance.get('active') and acceptance.get('blocked_bias') == side:
        adverse += 0.80
    memory_registry = getattr(snapshot, 'adverse_flow_memory_by_bias', {}) or {}
    memory = (
        dict(memory_registry.get(side) or {})
        if isinstance(memory_registry, dict) else {}
    )
    snapshot_now = _f(getattr(snapshot, 'snapshot_time', 0.0))
    memory_ts = _f(memory.get('ts'))
    memory_age = max(0.0, snapshot_now - memory_ts)
    memory_strength = (
        _clamp(memory.get('severity'))
        * math.exp(-math.log(2.0) * memory_age / 5.0)
        if (
            memory.get('blocked_bias') == side
            and memory_ts > 0.0 and memory_ts <= snapshot_now + 0.25
        ) else 0.0
    )
    adverse += 0.90 * memory_strength
    events = [
        _event(snapshot, name) for name in (
            'continuous_sweep_m1', 'continuous_breakout_m1',
            'continuous_footprint', 'continuous_persistent_flow',
            'continuous_zone_reaction', 'continuous_absorption_reaction',
        )
    ]
    fresh_quality = []
    now = _f(getattr(snapshot, 'snapshot_time', 0.0))
    for event in events:
        timestamp = _f(event.get('ts'))
        if not event or timestamp <= 0.0 or timestamp > now + 0.25:
            continue
        ttl = max(_f(event.get('ttl'), 15.0), 1.0)
        freshness = _clamp(1.0 - max(0.0, now - timestamp) / ttl)
        fresh_quality.append(_clamp(event.get('quality'), 0.0, 1.0) * freshness)
    data_confidence = _clamp(
        0.55 * confidence
        + 0.25 * (sum(fresh_quality) / len(fresh_quality) if fresh_quality else 0.0)
        + 0.20 * _clamp(getattr(snapshot, 'price_progress_coverage_3s', 0.0) / 3.0)
    )
    support = _clamp(
        0.35 * score_quality + 0.25 * confidence
        + 0.20 * activation + 0.20 * flow_response
    )
    return {
        'score_quality': score_quality, 'confidence': confidence,
        'activation': activation, 'flow_response': flow_response,
        'price_progress_atr': progress, 'data_confidence': data_confidence,
        'support': support, 'adverse': _clamp(adverse),
        'adverse_flow_memory': _clamp(memory_strength),
    }


def _evaluate_candidate(
    candidate, candidates, snapshot, signal, entry_price, stop_price,
    entry_fee_bps, exit_fee_bps, entry_slippage_bps, exit_slippage_bps,
):
    side = str(signal['bias'])
    entry = _f(entry_price)
    atr = max(_f(getattr(snapshot, 'atr_1m', 0.0)), 1e-9)
    target = _f(candidate['price'])
    distance = target - entry if side == 'LONG' else entry - target
    stop_distance = entry - _f(stop_price) if side == 'LONG' else _f(stop_price) - entry
    distance_bps = max(0.0, distance / entry * 10000.0)
    stop_bps = max(0.0, stop_distance / entry * 10000.0)
    distance_atr = max(0.0, distance / atr)
    # Compare a target with the volatility available over its estimated path,
    # not with a single one-minute ATR.  Horizon changes continuously so 2.01
    # ATR cannot jump to a different rule than 1.99 ATR.
    horizon = max(5.0, min(45.0, 5.0 + 4.0 * distance_atr))
    atr_normalized_distance = distance_atr / math.sqrt(horizon)
    continuation_meta = dict(candidate.get('continuation_meta', {}) or {})
    if continuation_meta:
        # Low-ATR impulses can travel many one-minute ATRs while completing a
        # normal measured leg. Normalize that hypothesis by the already known
        # structural leg, never by subsequent displacement.
        measured_normalized_distance = 0.75 + 0.65 * _clamp(
            continuation_meta.get('extension_ratio'), 0.0, 1.0
        )
        normalized_distance = min(
            atr_normalized_distance, measured_normalized_distance
        )
        path_normalization_basis = 'CAUSAL_MEASURED_LEG'
    else:
        normalized_distance = atr_normalized_distance
        path_normalization_basis = 'ATR_SQRT_HORIZON'
    context = _score_context(snapshot, signal, side)
    obstacles = sum(
        1 for item in candidates
        if item['rank'] < candidate['rank']
        and any(source in ('POC', 'VAH', 'VAL', 'M15_SWING') for source in item['sources'])
    )
    retest = str(signal.get('entry_style') or '').upper() == 'PASSIVE_RETEST'
    chase = bool(
        str(signal.get('setup_kind') or '').lower() == 'breakout' and not retest
    )
    logit = (
        -0.45 + 3.20 * context['support']
        + 0.55 * context['flow_response']
        + 0.30 * max(0.0, context['price_progress_atr'])
        + (0.25 if retest else 0.0)
        - 0.65 * normalized_distance
        - 0.30 * math.log1p(normalized_distance)
        - 0.22 * obstacles - 0.55 * float(chase)
        - 0.90 * context['adverse']
    )
    p_hit = _clamp(_sigmoid(logit), 0.02, 0.98)
    uncertainty = _clamp(
        0.06 + 0.16 * (1.0 - context['data_confidence'])
        + 0.04 * min(normalized_distance, 3.0)
        + 0.02 * (horizon / 45.0) + 0.025 * obstacles,
        0.06, 0.30,
    )
    p_hit_lcb = _clamp(p_hit - uncertainty)
    p_stop = _clamp((1.0 - p_hit) * (0.55 + 0.35 * context['adverse']))
    p_stop_ucb = _clamp(p_stop + 0.50 * uncertainty)
    costs = (
        max(0.0, _f(entry_fee_bps)) + max(0.0, _f(exit_fee_bps))
        + max(0.0, _f(entry_slippage_bps))
        + max(0.0, _f(exit_slippage_bps))
    )
    mean = p_hit * distance_bps - p_stop * stop_bps - costs
    lcb = p_hit_lcb * distance_bps - p_stop_ucb * stop_bps - costs
    return {
        **candidate,
        'distance_bps': distance_bps, 'distance_atr': distance_atr,
        'normalized_path_distance': normalized_distance,
        'atr_normalized_path_distance': atr_normalized_distance,
        'path_normalization_basis': path_normalization_basis,
        'stop_distance_bps': stop_bps, 'obstacle_count': obstacles,
        'p_hit_before_stop': p_hit, 'p_hit_lcb': p_hit_lcb,
        'p_stop': p_stop, 'p_stop_ucb': p_stop_ucb,
        'horizon_minutes': horizon, 'net_edge_mean': mean,
        'net_edge_lcb': lcb, 'uncertainty': uncertainty,
        'context': context,
    }


def _allocation_feasible(allocation, quantity, price, filters):
    if allocation <= 0.0:
        return True
    step = max(_f((filters or {}).get('step_size'), 0.001), 1e-12)
    min_qty = max(_f((filters or {}).get('min_qty'), step), step)
    min_notional = max(_f((filters or {}).get('min_notional')), 0.0)
    qty = math.floor((quantity * allocation + 1e-12) / step) * step
    return bool(qty >= min_qty and (not min_notional or qty * price >= min_notional))


def _optimize_pair(near, runner, quantity, filters):
    if runner['net_edge_lcb'] <= 0.0:
        return None
    upper = MAX_TP1_ALLOCATION if near['net_edge_lcb'] > 0.0 else 0.0
    correlation = _clamp(
        0.45 + 0.35 * min(1.0, near['distance_atr'] / max(runner['distance_atr'], 1e-9)),
        0.0, 0.90,
    )
    best = None
    # 0.1% resolution is materially continuous after exchange step rounding.
    for index in range(int(round(upper * 1000.0)) + 1):
        allocation = index / 1000.0
        if not _allocation_feasible(allocation, quantity, near['price'], filters):
            continue
        mean = allocation * near['net_edge_mean'] + (1.0 - allocation) * runner['net_edge_mean']
        u1 = near['uncertainty'] * near['distance_bps']
        u2 = runner['uncertainty'] * runner['distance_bps']
        epistemic = math.sqrt(max(
            0.0,
            (allocation * u1) ** 2 + ((1.0 - allocation) * u2) ** 2
            + 2.0 * correlation * allocation * (1.0 - allocation) * u1 * u2,
        ))
        probability_lcb = (
            allocation * near['net_edge_lcb']
            + (1.0 - allocation) * runner['net_edge_lcb']
        )
        lcb = probability_lcb - 0.10 * epistemic
        plan = {
            'tp1_allocation': allocation, 'net_edge_mean': mean,
            'net_edge_lcb': lcb, 'epistemic_buffer_bps': 0.10 * epistemic,
        }
        if best is None or (plan['net_edge_lcb'], plan['net_edge_mean']) > (
            best['net_edge_lcb'], best['net_edge_mean']
        ):
            best = plan
    return best


def _realizable_plan(best, all_in_cost_bps):
    """Conservative edge under the exit policy that will actually run live.

    A zero-allocation near target cannot lock profit and therefore cannot
    authorize a market entry.  When TP1 is executable, the runner receives a
    haircut because EMA trailing can exit it before the structural target.
    """
    allocation = _clamp(best.get('tp1_allocation'), 0.0, MAX_TP1_ALLOCATION)
    near = best['near']
    runner = best['runner']
    costs = max(0.0, _f(all_in_cost_bps))
    checkpoint_monetizable = bool(
        allocation > 0.0
        and _allocation_feasible(
            allocation, best.get('quantity', 0.0), near['price'],
            best.get('filters', {}),
        )
    )
    checkpoint_lock_gross = allocation * _f(near.get('distance_bps'))
    checkpoint_lock_net = checkpoint_lock_gross - costs

    if checkpoint_monetizable:
        near_capture = (
            allocation * _f(near.get('p_hit_lcb'))
            * _f(near.get('distance_bps'))
        )
        runner_capture = (
            (1.0 - allocation) * _f(runner.get('p_hit_lcb'))
            * _f(runner.get('distance_bps'))
            * _clamp(TRAILING_RUNNER_RETENTION_LCB)
        )
        # Once TP1 is monetized, the remaining runner trails from break-even;
        # only failure before the checkpoint retains full-stop downside.
        downside = (
            _f(near.get('p_stop_ucb'))
            * _f(near.get('stop_distance_bps'))
        )
        trailing_applies = True
    else:
        # No executable TP1 means no checkpoint/trailing side effect.  The
        # runner is managed as the sole structural exit.
        near_capture = 0.0
        runner_capture = (
            _f(runner.get('p_hit_lcb'))
            * _f(runner.get('distance_bps'))
        )
        downside = (
            _f(runner.get('p_stop_ucb'))
            * _f(runner.get('stop_distance_bps'))
        )
        trailing_applies = False

    epistemic = max(0.0, _f(best.get('epistemic_buffer_bps')))
    runner_dependency = 0.0 if checkpoint_monetizable else 1.0
    path_candidates = list(best.get('path_candidates') or ())
    local_candidates = [
        item for item in path_candidates
        if _f(item.get('distance_bps')) < _f(runner.get('distance_bps'))
    ]
    if not local_candidates:
        local_candidates = [near]

    def candidate_lcb(item):
        explicit = item.get('net_edge_lcb')
        if explicit is not None:
            return _f(explicit)
        return (
            _f(item.get('p_hit_lcb')) * _f(item.get('distance_bps'))
            - _f(item.get('p_stop_ucb')) * _f(item.get('stop_distance_bps'))
            - costs
        )

    # "Local" means the part of the path around the first economically useful
    # checkpoint, not every target that happens to be closer than the runner.
    # The old max() let a target 30-40 bps away certify a 60 bps runner while
    # all checkpoints near fee break-even were still negative.  A Gaussian
    # kernel keeps the assessment continuous and gives distant targets
    # vanishing authority without introducing another distance cliff.
    local_path_anchor_bps = max(
        costs + REALIZABLE_MARKET_BUFFER_BPS, 2.0,
    )
    local_path_bandwidth_bps = max(2.0, 0.35 * local_path_anchor_bps)
    weighted_local = []
    for item in local_candidates:
        distance = max(0.0, _f(item.get('distance_bps')))
        z_score = (
            (distance - local_path_anchor_bps)
            / local_path_bandwidth_bps
        )
        weight = math.exp(-0.5 * z_score * z_score)
        if weight > 1e-9:
            weighted_local.append((weight, candidate_lcb(item)))
    if weighted_local:
        weight_sum = sum(weight for weight, _ in weighted_local)
        local_path_lcb = sum(
            weight * value for weight, value in weighted_local
        ) / weight_sum
    else:
        local_path_lcb = candidate_lcb(near)
    local_confirmation = _sigmoid(local_path_lcb / 2.0)
    # When no checkpoint can actually monetize, a distant runner must pay an
    # extra extrapolation buffer. It is continuous: a healthier near path
    # smoothly removes the buffer instead of flipping a Boolean fee rule.
    extrapolation_exposure = min(
        costs,
        0.10 * _f(runner.get('distance_bps')),
    )
    runner_dependency_buffer = (
        runner_dependency * (1.0 - local_confirmation)
        * extrapolation_exposure
    )
    capture_lcb = near_capture + runner_capture
    edge_lcb = (
        capture_lcb - downside - costs - epistemic
        - runner_dependency_buffer
    )
    return {
        'realizable_capture_lcb_bps': capture_lcb,
        'realizable_stop_risk_ucb_bps': downside,
        'realizable_edge_lcb': edge_lcb,
        'checkpoint_monetizable': checkpoint_monetizable,
        'checkpoint_lock_gross_bps': checkpoint_lock_gross,
        'checkpoint_lock_net_bps': checkpoint_lock_net,
        'checkpoint_market_required_net_bps': REALIZABLE_MARKET_BUFFER_BPS,
        'trailing_applies_after_tp1': trailing_applies,
        'trailing_runner_retention_lcb': (
            _clamp(TRAILING_RUNNER_RETENTION_LCB)
            if trailing_applies else 1.0
        ),
        'runner_dependency': runner_dependency,
        'local_path_lcb': local_path_lcb,
        'local_path_confirmation': local_confirmation,
        'local_path_anchor_bps': local_path_anchor_bps,
        'local_path_bandwidth_bps': local_path_bandwidth_bps,
        'runner_extrapolation_exposure_bps': extrapolation_exposure,
        'runner_dependency_buffer_bps': runner_dependency_buffer,
    }


def _entry_policy(realizable):
    edge = _f(realizable.get('realizable_edge_lcb'))
    market_ready = bool(
        edge >= REALIZABLE_MARKET_BUFFER_BPS
        and realizable.get('checkpoint_monetizable')
        and _f(realizable.get('checkpoint_lock_net_bps'))
        >= REALIZABLE_MARKET_BUFFER_BPS
    )
    if edge <= 0.0:
        return 'BLOCK', 0.0
    if not market_ready:
        return 'PASSIVE_RETEST_ONLY', _clamp(math.sqrt(
            edge / max(MARKET_EDGE_BPS, 1e-9)
        ))
    return 'MARKET_OR_CONFIGURED', 1.0


def reassess_saved_plan(plan, quantity, filters=None):
    """Replay a persisted V2 plan under the realizable exit contract."""
    plan = dict(plan or {})
    selected = list(plan.get('selected_target_ids') or ())
    by_id = {
        item.get('target_id'): item
        for item in plan.get('target_candidates', ())
        if isinstance(item, dict)
    }
    if len(selected) != 2 or any(item not in by_id for item in selected):
        return {'available': False, 'reason': 'SAVED_PLAN_TARGETS_MISSING'}
    best = {
        'tp1_allocation': _f(plan.get('tp1_allocation')),
        'near': by_id[selected[0]], 'runner': by_id[selected[1]],
        'quantity': quantity, 'filters': filters or {},
        'epistemic_buffer_bps': _f(plan.get('epistemic_buffer_bps')),
        'path_candidates': list(plan.get('target_candidates') or ()),
    }
    realizable = _realizable_plan(best, _f(plan.get('all_in_cost_bps')))
    policy, multiplier = _entry_policy(realizable)
    return {
        'available': True, 'reason': 'PASS' if realizable['realizable_edge_lcb'] > 0 else 'NON_POSITIVE_REALIZABLE_EDGE',
        **realizable, 'entry_policy': policy,
        'economic_size_multiplier': multiplier,
    }


def plan_exit(
    snapshot, signal, quantity, entry_price, stop_price, tick_size=0.1,
    filters=None, entry_fee_bps=4.0, exit_fee_bps=4.0,
    entry_slippage_bps=0.0, exit_slippage_bps=0.0,
):
    """Return the best causal two-leg plan or a fail-closed explanation."""
    candidates = build_path_candidates(snapshot, signal, entry_price, tick_size)
    bootstrap_audit = _meta_bootstrap_audit(candidates)
    evaluated = [
        _evaluate_candidate(
            item, candidates, snapshot, signal, entry_price, stop_price,
            entry_fee_bps, exit_fee_bps, entry_slippage_bps,
            exit_slippage_bps,
        )
        for item in candidates
    ]
    if len(evaluated) < 2:
        return {
            'version': VERSION, 'model_version': MODEL_VERSION,
            'feature_schema': FEATURE_SCHEMA, 'available': False,
            'reason': 'INSUFFICIENT_STRUCTURAL_TARGETS',
            'target_candidates': evaluated, 'economic_pass': False,
            'meta_bootstrap': bootstrap_audit,
        }
    best = None
    for near_index in range(len(evaluated) - 1):
        near = evaluated[near_index]
        for runner in evaluated[near_index + 1:]:
            optimized = _optimize_pair(near, runner, quantity, filters or {})
            if optimized is None:
                continue
            proposal = {
                **optimized, 'near': near, 'runner': runner,
                'quantity': quantity, 'filters': filters or {},
                'path_candidates': evaluated,
            }
            all_in = (
                _f(entry_fee_bps) + _f(exit_fee_bps)
                + _f(entry_slippage_bps) + _f(exit_slippage_bps)
            )
            proposal.update(_realizable_plan(proposal, all_in))
            if best is None or (
                proposal['realizable_edge_lcb'], proposal['net_edge_lcb']
            ) > (
                best['realizable_edge_lcb'], best['net_edge_lcb']
            ):
                best = proposal
    if best is None:
        return {
            'version': VERSION, 'model_version': MODEL_VERSION,
            'feature_schema': FEATURE_SCHEMA, 'available': False,
            'reason': 'NO_POSITIVE_RUNNER_PATH',
            'target_candidates': evaluated, 'economic_pass': False,
            'meta_bootstrap': bootstrap_audit,
        }
    edge_lcb = _f(best['net_edge_lcb'])
    realizable_edge_lcb = _f(best['realizable_edge_lcb'])
    entry_policy, size_multiplier = _entry_policy(best)
    return {
        'version': VERSION, 'model_version': MODEL_VERSION,
        'feature_schema': FEATURE_SCHEMA, 'available': True,
        'reason': (
            'PASS' if realizable_edge_lcb > 0.0
            else 'NON_POSITIVE_REALIZABLE_EDGE'
        ),
        'economic_pass': realizable_edge_lcb > 0.0,
        'entry_policy': entry_policy,
        'economic_size_multiplier': size_multiplier,
        'passive_size_cap_pct': PASSIVE_SIZE_CAP_PCT,
        'tp1': best['near']['price'],
        'tp1_allocation': round(best['tp1_allocation'], 4),
        'runner_target': best['runner']['price'],
        'runner_allocation': round(1.0 - best['tp1_allocation'], 4),
        'runner_policy': 'ADAPTIVE_STRUCTURE_TRAIL_V1',
        'net_edge_mean': best['net_edge_mean'],
        'net_edge_lcb': edge_lcb,
        'realizable_edge_lcb': realizable_edge_lcb,
        'realizable_capture_lcb_bps': best['realizable_capture_lcb_bps'],
        'realizable_stop_risk_ucb_bps': best['realizable_stop_risk_ucb_bps'],
        'checkpoint_monetizable': best['checkpoint_monetizable'],
        'checkpoint_lock_gross_bps': best['checkpoint_lock_gross_bps'],
        'checkpoint_lock_net_bps': best['checkpoint_lock_net_bps'],
        'checkpoint_market_required_net_bps': REALIZABLE_MARKET_BUFFER_BPS,
        'trailing_applies_after_tp1': best['trailing_applies_after_tp1'],
        'trailing_runner_retention_lcb': best['trailing_runner_retention_lcb'],
        'runner_dependency': best['runner_dependency'],
        'local_path_lcb': best['local_path_lcb'],
        'local_path_confirmation': best['local_path_confirmation'],
        'local_path_anchor_bps': best['local_path_anchor_bps'],
        'local_path_bandwidth_bps': best['local_path_bandwidth_bps'],
        'runner_extrapolation_exposure_bps': (
            best['runner_extrapolation_exposure_bps']
        ),
        'runner_dependency_buffer_bps': best['runner_dependency_buffer_bps'],
        'epistemic_buffer_bps': best['epistemic_buffer_bps'],
        'all_in_cost_bps': (
            _f(entry_fee_bps) + _f(exit_fee_bps)
            + _f(entry_slippage_bps) + _f(exit_slippage_bps)
        ),
        'entry_fee_bps': _f(entry_fee_bps),
        'exit_fee_bps': _f(exit_fee_bps),
        'target_candidates': evaluated,
        'meta_bootstrap': bootstrap_audit,
        'selected_target_ids': [best['near']['target_id'], best['runner']['target_id']],
        'live_authority': True,
    }
