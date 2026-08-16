"""Pure bounded action selection; no order/exchange side effects."""

import math


def _f(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def required_notional_pct(equity, btc_price, minimum_qty=0.001):
    if _f(equity) <= 0.0 or _f(btc_price) <= 0.0:
        return float('inf')
    return minimum_qty * _f(btc_price) / _f(equity) * 100.0


def theoretical_size_pct(utility_lcb, reference_utility, ood_reliability, cap=1.5):
    quality = max(0.0, min(1.0, _f(utility_lcb) / max(_f(reference_utility), 1e-9)))
    return min(_f(cap), _f(cap) * quality * max(0.0, min(1.0, _f(ood_reliability))))


def choose_action(actions, switching_cost_bps=0.0, incumbent=None):
    """LCB dominance; ties conservatively prefer WATCH, then maker, then market."""
    viable = [row for row in actions if _f(row.get('utility_lcb')) > 0.0]
    if not viable:
        return {'action_id': 'WATCH', 'kind': 'WATCH', 'utility_lcb': 0.0}
    rank = {'WATCH': 0, 'MAKER': 1, 'MARKET': 2}
    viable.sort(key=lambda row: (-_f(row.get('utility_lcb')), rank.get(row.get('kind'), 3)))
    challenger = viable[0]
    if incumbent:
        required = _f(incumbent.get('utility_ucb')) + max(0.0, _f(switching_cost_bps))
        if _f(challenger.get('utility_lcb')) <= required:
            return incumbent
    if challenger.get('kind') == 'MARKET':
        makers = [row for row in viable if row.get('kind') == 'MAKER']
        if makers:
            best_maker = max(makers, key=lambda row: _f(row.get('utility_ucb')))
            if _f(challenger.get('utility_lcb')) <= (
                _f(best_maker.get('utility_ucb')) + max(0.0, _f(switching_cost_bps))
            ):
                return best_maker
    return challenger


def executable_size(theoretical_pct, equity, btc_price, minimum_qty=0.001, cap=1.5):
    pct = min(max(0.0, _f(theoretical_pct)), _f(cap))
    required = required_notional_pct(equity, btc_price, minimum_qty)
    if pct + 1e-12 < required:
        return {'action': 'WATCH', 'size_pct': 0.0, 'required_pct': required,
                'reason': 'BELOW_EXCHANGE_MIN_WITHOUT_UPSIZE'}
    return {'action': 'EXECUTABLE', 'size_pct': pct, 'required_pct': required,
            'quantity': minimum_qty}
