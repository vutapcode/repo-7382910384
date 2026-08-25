"""Hierarchical empirical expectancy calibrator persisted by runtime state."""
import math

VERSION = "EDGE_CAL_V6_COST_TELEMETRY"
MAX_ROWS = 768


def _u(value, default):
    return str(value or default).upper()


def _normalize(row):
    """Upgrade legacy rows without letting them authorize known cohorts."""
    if len(row) >= 9:
        cost = None if row[8] is None else float(row[8])
        return tuple(row[:7]) + (float(row[7]), cost)
    if len(row) >= 8:
        return tuple(row[:7]) + (float(row[7]), None)
    if len(row) == 5:
        side, mode, regime, edge_class, net_bps = row
        return (side, mode, regime, edge_class,
                "UNKNOWN", "UNKNOWN", "UNKNOWN", float(net_bps), None)
    return None


def _rows(state):
    rows = getattr(state, "_edge_cal_v2_rows", None)
    if rows is None:
        rows = []
        state._edge_cal_v2_rows = rows
    normalized = [item for item in (_normalize(row) for row in rows) if item]
    if normalized != rows:
        rows[:] = normalized
    if len(rows) > MAX_ROWS:
        del rows[:-MAX_ROWS]
    return rows


def record(state, mode, regime, net_bps, side=None, edge_class=None,
           proof_type=None, proposer=None, execution_style=None,
           execution_cost_bps=None):
    cost = None if execution_cost_bps is None else float(execution_cost_bps)
    row = (_u(side, "UNKNOWN"), _u(mode, "NORMAL"),
           _u(regime, "NORMAL"), _u(edge_class, "UNKNOWN"),
           _u(proof_type, "UNKNOWN"), _u(proposer, "UNKNOWN"),
           _u(execution_style, "UNKNOWN"), float(net_bps), cost)
    _rows(state).append(row)
    state.edge_cal_v2_last = row
    return row


def _vals(rows, key, level):
    side, _mode, regime, _edge, proof, _proposer, execution = key
    if level == "EXACT":
        selected = [row for row in rows if row[:7] == key]
    elif level == "SIDE_REGIME_PROOF_EXEC":
        selected = [row for row in rows if row[0] == side and
                    row[2] == regime and row[4] == proof and row[6] == execution]
    elif level == "REGIME_PROOF_EXEC":
        selected = [row for row in rows if
                    row[2] == regime and row[4] == proof and row[6] == execution]
    elif level == "PROOF_EXEC":
        selected = [row for row in rows if row[4] == proof and row[6] == execution]
    else:
        selected = []
    return [row[7] for row in selected]


def _cost_values(rows, key):
    return [
        float(row[8]) for row in rows
        if row[:7] == key and len(row) >= 9 and row[8] is not None
    ]


def _percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * float(q)
    lo, hi = int(math.floor(index)), int(math.ceil(index))
    if lo == hi:
        return ordered[lo]
    weight = index - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _cost_distribution(values):
    if not values:
        return None
    return {
        "min": round(min(values), 4),
        "mean": round(sum(values) / len(values), 4),
        "p50": round(_percentile(values, 0.50), 4),
        "p90": round(_percentile(values, 0.90), 4),
        "max": round(max(values), 4),
    }


def _estimate(values, bound):
    values = sorted(values[-256:])
    trim = max(1, int(len(values) * .1))
    core = values[trim:-trim] if len(values) > 2 * trim else values
    mean = sum(core) / len(core)
    win_rate = sum(value > 0 for value in values) / len(values)
    factor_value = 1 + max(-bound, min(bound, mean / 250))
    if win_rate < .44:
        factor_value = min(factor_value, .97)
    elif win_rate > .61:
        factor_value = max(factor_value, 1.02)
    variance = sum((value - mean) ** 2 for value in core) / max(1, len(core) - 1)
    stderr = math.sqrt(max(0.0, variance)) / math.sqrt(max(1, len(core)))
    lower_bound = mean - 1.645 * stderr
    return max(1 - bound, min(1 + bound, factor_value)), mean, win_rate, lower_bound


def factor(state, mode, regime, side=None, edge_class=None, proof_type=None,
           proposer=None, execution_style=None):
    key = (_u(side, "UNKNOWN"), _u(mode, "NORMAL"),
           _u(regime, "NORMAL"), _u(edge_class, "UNKNOWN"),
           _u(proof_type, "UNKNOWN"), _u(proposer, "UNKNOWN"),
           _u(execution_style, "UNKNOWN"))
    rows = list(_rows(state))
    exact_costs = _cost_values(rows, key)
    cost_distribution = _cost_distribution(exact_costs)
    levels = (("EXACT", 30, .08), ("SIDE_REGIME_PROOF_EXEC", 40, .06),
              ("REGIME_PROOF_EXEC", 48, .05), ("PROOF_EXEC", 64, .04))
    for level, minimum, bound in levels:
        values = _vals(rows, key, level)
        if len(values) >= minimum:
            factor_value, mean, win_rate, lower = _estimate(values, bound)
            out = {
                "version": VERSION, "samples": len(values),
                "factor": round(factor_value, 4), "mean_net_bps": round(mean, 4),
                "lower_confidence_bound_bps": round(lower, 4),
                "win_rate": round(win_rate, 4), "status": "ACTIVE",
                "live_empirical_ok": bool(mean > 0.0 and lower >= 0.0),
                "level": level, "minimum_samples": minimum,
                "max_adjust": bound, "bucket": "|".join(key),
                "total_samples": len(rows),
                "execution_cost_samples": len(exact_costs),
                "execution_cost_distribution_bps": cost_distribution,
                "execution_cost_authority": False,
            }
            state.edge_cal_v2 = out
            return out
    out = {"version": VERSION, "samples": len(_vals(rows, key, "EXACT")),
           "factor": 1.0, "status": "INSUFFICIENT_DATA",
           "live_empirical_ok": False, "level": "NONE", "minimum_samples": 30,
           "bucket": "|".join(key), "total_samples": len(rows),
           "execution_cost_samples": len(exact_costs),
           "execution_cost_distribution_bps": cost_distribution,
           "execution_cost_authority": False}
    state.edge_cal_v2 = out
    return out
