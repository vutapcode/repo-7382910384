"""Trade-relative Cash/Perp context; never objective Spot price discovery."""
VERSION = "FLOW_LEAD_ENGINE_V2_ALIGNED"
MAX_SKEW_S = 0.30

def _f(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0

def _flow_mean(row):
    vals = [
        _f(v.get("signed_imbalance"))
        for v in (row.get("venues") or {}).values()
        if isinstance(v, dict)
    ]
    return sum(vals) / len(vals) if vals else 0.0

def _ref(rows, now, age):
    target = now - age
    out = None
    for row in rows:
        if _f(row.get("ts")) <= target:
            out = row
        else:
            break
    return out

def _signed_bps(cur, ref, side):
    cur, ref = _f(cur), _f(ref)
    if cur <= 0.0 or ref <= 0.0:
        return None
    direction = 1.0 if str(side).upper() == "LONG" else -1.0
    return (cur - ref) / ref * 10000.0 * direction

def _freshness(state):
    spot = _f(getattr(state, "thoi_gian_tick_cuoi", 0.0))
    coinbase = _f(getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0))
    futures = _f(getattr(state, "execution_price_time", 0.0))
    values = [v for v in (spot, coinbase, futures) if v > 0.0]
    if len(values) < 3:
        return {"aligned": False, "skew_s": None}
    skew = max(values) - min(values)
    return {"aligned": skew <= MAX_SKEW_S, "skew_s": round(skew, 4)}

def analyze(state, side):
    """Return context only; never creates a trade signal."""
    flow_rows = list(getattr(state, "entry_causal_flow_history", ()) or ())[-24:]
    price_rows = list(getattr(state, "entry_shadow_price_history", ()) or ())[-32:]
    fresh = _freshness(state)

    if not flow_rows or not price_rows:
        return {
            "version": VERSION, "status": "WARMUP", "persistence": 0.0,
            "oppose_ratio": 0.0, "lead": "UNKNOWN", "lead_gap_bps": 0.0,
            "lead_accel_bps": 0.0, "freshness": fresh,
            "lead_scope": "TRADE_RELATIVE_CASH_VS_PERP",
            "spot_price_discovery": "NOT_MEASURED_HERE",
        }

    recent = flow_rows[-12:]
    means = [_flow_mean(r) for r in recent]
    persistence = sum(v >= 0.08 for v in means) / len(means) if means else 0.0
    oppose_ratio = sum(v <= -0.08 for v in means) / len(means) if means else 0.0

    if not fresh["aligned"]:
        return {
            "version": VERSION, "status": "UNALIGNED",
            "persistence": round(persistence, 4),
            "oppose_ratio": round(oppose_ratio, 4),
            "flow_mean": round(sum(means) / len(means), 4) if means else 0.0,
            "lead": "UNKNOWN", "lead_gap_bps": 0.0, "lead_accel_bps": 0.0,
            "freshness": fresh,
            "lead_scope": "TRADE_RELATIVE_CASH_VS_PERP",
            "spot_price_discovery": "NOT_MEASURED_HERE",
            "policy": "NO_LEAD_INFERENCE_WHEN_FEEDS_ARE_TIME_SKEWED",
        }

    now_row = price_rows[-1]
    now = _f(now_row.get("ts"))
    fast_ref = _ref(price_rows, now, 0.35)
    slow_ref = _ref(price_rows, now, 0.80)

    def gap(ref):
        if not ref:
            return 0.0
        spot = _signed_bps(now_row.get("spot"), ref.get("spot"), side)
        cb = _signed_bps(now_row.get("coinbase"), ref.get("coinbase"), side)
        fut = _signed_bps(now_row.get("futures"), ref.get("futures"), side)
        cash_vals = [x for x in (spot, cb) if x is not None]
        if fut is None or not cash_vals:
            return 0.0
        cash = sum(cash_vals) / len(cash_vals)
        return fut - cash

    fast_gap = gap(fast_ref)
    slow_gap = gap(slow_ref)
    accel = fast_gap - slow_gap

    if fast_gap >= 1.25:
        lead = "PERP_LED"
    elif fast_gap <= -0.75:
        lead = "CASH_LED"
    else:
        lead = "BALANCED"

    return {
        "version": VERSION,
        "status": "OK",
        "persistence": round(persistence, 4),
        "oppose_ratio": round(oppose_ratio, 4),
        "flow_mean": round(sum(means) / len(means), 4) if means else 0.0,
        "lead": lead,
        "lead_gap_bps": round(fast_gap, 4),
        "lead_accel_bps": round(accel, 4),
        "freshness": fresh,
        "lead_scope": "TRADE_RELATIVE_CASH_VS_PERP",
        "spot_price_discovery": "NOT_MEASURED_HERE",
        "policy": "CONTEXT_ONLY_TIME_ALIGNED_NO_SIGNAL_AUTHORITY",
    }
