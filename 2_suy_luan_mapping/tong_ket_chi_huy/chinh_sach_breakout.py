"""Pure breakout economics: meaningful targets, never ATR-as-edge."""

import hashlib
import os


ENTRY_FEE_BPS = float(os.getenv('SMC_SHADOW_ENTRY_FEE_BPS', '4.0'))
EXIT_FEE_BPS = float(os.getenv('SMC_SHADOW_EXIT_FEE_BPS', '4.0'))
SAFETY_MARGIN_BPS = float(os.getenv('SMC_MIN_NET_EDGE_BPS', '4.0'))
CAPTURE_RATIO = float(os.getenv('SMC_CAPTURE_RATIO', '0.60'))


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _candle_high_low(candle):
    if isinstance(candle, dict):
        return _f(candle.get('h') or candle.get('high')), _f(
            candle.get('l') or candle.get('low')
        )
    if isinstance(candle, (list, tuple)) and len(candle) >= 4:
        return _f(candle[2]), _f(candle[3])
    return 0.0, 0.0


def _local_extrema(klines, side):
    rows = [_candle_high_low(item) for item in list(klines or ())[-96:]]
    field = 0 if side == 'LONG' else 1
    values = []
    for index in range(1, len(rows) - 1):
        value = rows[index][field]
        before = rows[index - 1][field]
        after = rows[index + 1][field]
        if value <= 0.0 or before <= 0.0 or after <= 0.0:
            continue
        if (side == 'LONG' and value >= before and value >= after) or (
            side == 'SHORT' and value <= before and value <= after
        ):
            values.append(value)
    return values


def minimum_raw_target_bps(
    entry_slippage_bps=0.0, exit_slippage_bps=0.0,
    capture_ratio=None,
):
    """Raw distance required so expected capture still clears all costs.

    Costs are additive. Subtracting slippage or the safety margin would make a
    worse execution easier to approve, which is economically inverted.
    """
    ratio = CAPTURE_RATIO if capture_ratio is None else float(capture_ratio)
    ratio = min(1.0, max(1e-9, ratio))
    required_capture = (
        ENTRY_FEE_BPS + EXIT_FEE_BPS + SAFETY_MARGIN_BPS
        + max(0.0, float(entry_slippage_bps or 0.0))
        + max(0.0, float(exit_slippage_bps or 0.0))
    )
    return required_capture / ratio


def opportunity_base_key(state, direction, breakout_level, regime=''):
    raw = '|'.join((
        str(getattr(state, 'symbol', 'BTCUSDT') or 'BTCUSDT'),
        str(direction),
        f'{float(breakout_level):.8f}',
        str(getattr(state, 'structure_broken_level', 0.0) or breakout_level),
        str(regime or getattr(state, 'trend_m15', 'NEUTRAL')),
    ))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]


def evaluate(state, entry_price, side, tick_size=0.1):
    """Return the nearest real liquidity ladder and market-chase eligibility."""
    entry = _f(entry_price)
    tick = max(_f(tick_size, 0.1), 1e-9)
    if entry <= 0.0 or side not in ('LONG', 'SHORT'):
        return {
            'target': 0.0, 'target2': 0.0,
            'target_basis': 'BREAKOUT_NO_MEANINGFUL_LIQUIDITY_TARGET',
            'target_distance_bps': 0.0,
            'minimum_raw_target_bps': minimum_raw_target_bps(),
            'market_chase_allowed': False,
        }

    explicit = (
        (
            _f(getattr(state, 'swing_high_m15', 0.0)),
            _f(getattr(state, 'vah', 0.0)),
        ) if side == 'LONG' else (
            _f(getattr(state, 'swing_low_m15', 0.0)),
            _f(getattr(state, 'val', 0.0)),
        )
    )
    candidates = list(explicit)
    candidates.extend(_local_extrema(getattr(state, 'klines_m15', ()), side))
    candidates.extend(_local_extrema(list(getattr(state, 'klines_m1', ()) or ())[-60:], side))
    favorable = sorted({
        round(value / tick) * tick
        for value in candidates
        if (
            value >= entry + 2.0 * tick
            if side == 'LONG' else 0.0 < value <= entry - 2.0 * tick
        )
    }, reverse=(side == 'SHORT'))
    target = favorable[0] if favorable else 0.0
    target2 = favorable[1] if len(favorable) > 1 else 0.0
    distance = (
        (target - entry) if side == 'LONG' else (entry - target)
    ) if target > 0.0 else 0.0
    distance_bps = distance / entry * 10000.0 if entry > 0.0 else 0.0
    required = minimum_raw_target_bps()
    return {
        'target': target,
        'target2': target2,
        'target_basis': (
            'BREAKOUT_NEAREST_LIQUIDITY_EXTREMUM'
            if target > 0.0 else 'BREAKOUT_NO_MEANINGFUL_LIQUIDITY_TARGET'
        ),
        'target_distance_bps': distance_bps,
        'minimum_raw_target_bps': required,
        'market_chase_allowed': bool(target > 0.0 and distance_bps >= required),
    }
