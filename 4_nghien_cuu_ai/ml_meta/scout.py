"""Pure, bounded Opportunity Scout and action-ladder construction.

The Scout deliberately has no exchange/API dependency.  It creates causal
research rows for both sides and never decides that an order may be sent.
"""

import hashlib
import json
import math


SCHEMA_VERSION = 'ML_META_FEATURES_V1'
POLICY_VERSION = 'OPPORTUNITY_SCOUT_V1_5'
SIDES = ('LONG', 'SHORT')


def _f(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _round_tick(price, tick, side=None, maker=False):
    if price <= 0.0 or tick <= 0.0:
        return 0.0
    units = price / tick
    if maker and side == 'LONG':
        units = math.floor(units + 1e-9)
    elif maker and side == 'SHORT':
        units = math.ceil(units - 1e-9)
    else:
        units = round(units)
    return round(units * tick, 10)


def _archetype(mode):
    value = str(mode or '').upper()
    if 'BREAKOUT' in value:
        return 'BREAKOUT'
    if 'PULLBACK' in value:
        return 'PULLBACK'
    if 'FADE' in value:
        return 'REVERSAL'
    return 'NEUTRAL_MOMENTUM'


def _regime(snapshot, mode):
    trend = str(getattr(snapshot, 'trend_m15', 'NEUTRAL') or 'NEUTRAL').upper()
    value = str(mode or '').upper()
    if 'TRANSITION' in value:
        return 'TRANSITION'
    if trend in ('BULLISH', 'BEARISH'):
        return 'TREND'
    return 'RANGE_NEUTRAL'


def _anchor_candidates(snapshot):
    values = []
    for source, value in (
        ('VAH', getattr(snapshot, 'vah', 0.0)),
        ('VAL', getattr(snapshot, 'val', 0.0)),
        ('M15_SWING_HIGH', getattr(snapshot, 'swing_high_m15', 0.0)),
        ('M15_SWING_LOW', getattr(snapshot, 'swing_low_m15', 0.0)),
        ('STRUCTURE_BREAK', getattr(snapshot, 'structure_broken_level', 0.0)),
    ):
        price = _f(value)
        if price > 0.0:
            values.append((source, price))
    for row in list(getattr(snapshot, 'closed_m1_extrema', ()) or ())[-12:]:
        high, low = _f(row.get('high')), _f(row.get('low'))
        if high > 0.0:
            values.append(('M1_SWING_HIGH', high))
        if low > 0.0:
            values.append(('M1_SWING_LOW', low))
    return values


def select_anchor(snapshot, side):
    bid, ask = _f(getattr(snapshot, 'best_bid', 0.0)), _f(
        getattr(snapshot, 'best_ask', 0.0)
    )
    mid = (bid + ask) / 2.0 if ask > bid > 0.0 else max(bid, ask)
    candidates = _anchor_candidates(snapshot)
    if side == 'LONG':
        eligible = [(source, price) for source, price in candidates if price <= mid]
    else:
        eligible = [(source, price) for source, price in candidates if price >= mid]
    if not eligible:
        return {'available': False, 'price': None, 'source': None}
    source, price = min(eligible, key=lambda item: abs(item[1] - mid))
    return {'available': True, 'price': price, 'source': source}


def opportunity_identity(snapshot, side, anchor, tick_size):
    rounded = _round_tick(_f(anchor.get('price')), max(tick_size, 1e-9))
    raw = '|'.join((
        str(getattr(snapshot, 'symbol', 'BTCUSDT') or 'BTCUSDT'), side,
        str(anchor.get('source') or 'NO_ANCHOR'), f'{rounded:.8f}',
        str(int(getattr(snapshot, 'structure_version', 0) or 0)),
    ))
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f'BTCUSDT:SCOUT:{side}:{digest}'


def action_ladder(snapshot, side, anchor, tick_size):
    """Return a symmetric, de-duplicated maker ladder plus market action."""
    bid, ask = _f(snapshot.best_bid), _f(snapshot.best_ask)
    if bid <= 0.0 or ask <= 0.0 or tick_size <= 0.0:
        return []
    bbo = bid if side == 'LONG' else ask
    anchor_price = _f(anchor.get('price')) if anchor.get('available') else 0.0
    maker_prices = [('A0_POST_BBO', bbo)]
    maker_prices.append((
        'A1_POST_1_TICK', bbo - tick_size if side == 'LONG' else bbo + tick_size
    ))
    if anchor_price > 0.0:
        maker_prices.extend((
            ('A2_POST_25_ANCHOR', bbo + 0.25 * (anchor_price - bbo)),
            ('A3_POST_50_ANCHOR', bbo + 0.50 * (anchor_price - bbo)),
            ('A4_POST_ANCHOR', anchor_price),
        ))
    result, seen = [], set()
    book = list(
        getattr(snapshot, 'bids_top_10' if side == 'LONG' else 'asks_top_10', ())
        or ()
    )
    for action_id, price in maker_prices:
        rounded = _round_tick(price, tick_size, side=side, maker=True)
        # GTX invariant: a LONG maker cannot cross ask; SHORT cannot cross bid.
        if rounded <= 0.0 or (side == 'LONG' and rounded >= ask) or (
            side == 'SHORT' and rounded <= bid
        ):
            continue
        key = int(round(rounded / tick_size))
        if key in seen:
            continue
        seen.add(key)
        queue_ahead = 0.0
        for level in book:
            try:
                level_price, level_qty = float(level[0]), float(level[1])
            except (IndexError, TypeError, ValueError):
                continue
            if abs(level_price - rounded) <= tick_size * 0.25:
                queue_ahead = max(0.0, level_qty)
                break
        result.append({
            'action_id': action_id, 'kind': 'MAKER', 'price': rounded,
            'distance_from_bbo_ticks': round(abs(rounded - bbo) / tick_size, 4),
            'fee_schedule': 'MAKER', 'post_only': True,
            'queue_ahead_qty': round(queue_ahead, 8),
        })
    market = ask if side == 'LONG' else bid
    result.append({
        'action_id': 'A5_MARKET', 'kind': 'MARKET', 'price': market,
        'distance_from_bbo_ticks': 0.0, 'fee_schedule': 'TAKER',
        'post_only': False, 'queue_ahead_qty': 0.0,
    })
    return result


def causal_features(snapshot, score, side, anchor):
    momentum = dict(score.get('momentum_breakdown', {}) or {})
    selected = dict((score.get('sides', {}) or {}).get(side, {}) or {})
    price = (_f(snapshot.best_bid) + _f(snapshot.best_ask)) / 2.0
    atr = max(_f(getattr(snapshot, 'atr_1m', 0.0)), 1e-9)
    bid, ask = _f(snapshot.best_bid), _f(snapshot.best_ask)
    return {
        'side_sign': 1.0 if side == 'LONG' else -1.0,
        'atr_bps': atr / max(price, 1e-9) * 10000.0,
        'spread_bps': (ask - bid) / max(price, 1e-9) * 10000.0,
        'anchor_distance_atr': (
            abs(price - _f(anchor.get('price'))) / atr
            if anchor.get('available') else None
        ),
        'score': _f(selected.get('score')),
        'confidence': _f(selected.get('confidence')),
        'activation': _f(selected.get('activation')),
        'trade_power': _f(selected.get('trade_power')),
        'momentum_state': _f(score.get('momentum_state')),
        'momentum_price': _f(momentum.get('price')),
        'momentum_flow': _f(momentum.get('flow')),
        'momentum_acceptance': _f(momentum.get('acceptance')),
        'momentum_coverage': _f(momentum.get('coverage')),
        'microflow_timing': _f(selected.get('microflow_timing')),
        'impulse_conflict': _f(selected.get('impulse_conflict')),
        'retest_fit': _f(selected.get('retest_fit')),
        'obi_top3': _f(getattr(snapshot, 'obi_top3', 0.0)),
        'obi_top10': _f(getattr(snapshot, 'obi_top10', 0.0)),
        'cvd_buy_3s': _f(getattr(snapshot, 'current_cvd_buy_3s', 0.0)),
        'cvd_sell_3s': _f(getattr(snapshot, 'current_cvd_sell_3s', 0.0)),
        'vol_3s': _f(getattr(snapshot, 'current_vol_3s', 0.0)),
        'structure_strength': _f(
            (getattr(snapshot, 'continuous_m15', {}) or {}).get('trend_strength')
        ),
        'data_confidence': _f(score.get('data_confidence')),
    }


def build_rows(snapshot, score, mode, tick_size, run_id=None, code_version=None):
    rows = []
    for side in SIDES:
        anchor = select_anchor(snapshot, side)
        opportunity_id = opportunity_identity(snapshot, side, anchor, tick_size)
        rows.append({
            'schema_version': SCHEMA_VERSION,
            'policy_version': POLICY_VERSION,
            'decision_time': _f(snapshot.snapshot_time),
            'run_id': run_id,
            'code_version': code_version,
            'scorer_version': score.get('version'),
            'opportunity_id': opportunity_id,
            'side': side,
            'archetype': _archetype(mode),
            'regime': _regime(snapshot, mode),
            'structural_anchor': anchor.get('price'),
            'anchor_source': anchor.get('source'),
            'decision_bid': _f(snapshot.best_bid),
            'decision_ask': _f(snapshot.best_ask),
            'causal_features': causal_features(snapshot, score, side, anchor),
            'action_candidates': action_ladder(snapshot, side, anchor, tick_size),
            'data_quality': {
                'train_eligible': bool(anchor.get('available')),
                'flags': [] if anchor.get('available') else ['NO_STRUCTURAL_ANCHOR'],
            },
            'live_authority': False,
        })
    return rows


def row_hash(row):
    payload = dict(row)
    payload.pop('provenance', None)
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()
