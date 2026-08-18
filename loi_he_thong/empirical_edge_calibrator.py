"""Bounded RAM-only empirical edge calibrator for Tier-S shadow outcomes."""
from collections import deque

VERSION = "EDGE_CALIBRATOR_V1"
MIN_SAMPLES = 30
MAX_SAMPLES = 512

def _store(state):
    rows = getattr(state, "_tier_s_edge_outcomes", None)
    if rows is None:
        rows = deque(maxlen=MAX_SAMPLES)
        state._tier_s_edge_outcomes = rows
    return rows

def record(state, mode, regime, net_bps):
    row = {
        "mode": str(mode or "NORMAL").upper(),
        "regime": str(regime or "NORMAL").upper(),
        "net_bps": float(net_bps),
    }
    _store(state).append(row)
    state.tier_s_edge_calibration_last = row
    return row

def factor(state, mode, regime):
    mode = str(mode or "NORMAL").upper()
    regime = str(regime or "NORMAL").upper()
    rows = [
        r for r in list(_store(state))
        if r.get("mode") == mode and r.get("regime") == regime
    ]
    if len(rows) < MIN_SAMPLES:
        out = {
            "version": VERSION, "samples": len(rows), "factor": 1.0,
            "mean_net_bps": None, "win_rate": None,
            "status": "INSUFFICIENT_DATA",
        }
        state.tier_s_edge_calibration = out
        return out

    values = sorted(float(r["net_bps"]) for r in rows[-256:])
    trim = max(1, int(len(values) * 0.10))
    core = values[trim:-trim] if len(values) > trim * 2 else values
    mean_net = sum(core) / max(len(core), 1)
    win_rate = sum(v > 0 for v in values) / len(values)

    # Conservative, bounded adaptation. Never creates a signal or bypasses causal vetoes.
    raw = 1.0 + max(-0.10, min(0.10, mean_net / 200.0))
    if win_rate < 0.45:
        raw = min(raw, 0.95)
    elif win_rate > 0.60:
        raw = max(raw, 1.03)
    adj = max(0.90, min(1.10, raw))
    out = {
        "version": VERSION, "samples": len(values), "factor": round(adj, 4),
        "mean_net_bps": round(mean_net, 4), "win_rate": round(win_rate, 4),
        "status": "ACTIVE",
        "policy": "BOUNDED_EXPECTANCY_ONLY_NO_SIGNAL_AUTHORITY",
    }
    state.tier_s_edge_calibration = out
    return out
