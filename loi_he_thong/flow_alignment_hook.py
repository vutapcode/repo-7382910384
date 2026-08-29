"""Two-tier local-arrival alignment guard for cross-venue lead inference."""
import time

VERSION = "FLOW_ALIGNMENT_HOOK_V1"
HARD_SKEW_S = 0.30
SOFT_SKEW_S = 0.70
MAX_SOFT_AGE_S = 0.75


def _f(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def install(flow_lead_module):
    if getattr(flow_lead_module, "_alignment_hooked", False):
        return

    original_analyze = flow_lead_module.analyze

    def freshness(state):
        now = time.time()
        values = {
            "spot": _f(getattr(state, "thoi_gian_tick_cuoi", 0.0)),
            "coinbase": _f(getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0)),
            "futures": _f(getattr(state, "execution_price_time", 0.0)),
        }
        if any(v <= 0.0 for v in values.values()):
            return {"aligned": False, "mode": "MISSING", "skew_s": None}

        stamps = list(values.values())
        skew = max(stamps) - min(stamps)
        ages = {name: max(0.0, now - ts) for name, ts in values.items()}
        max_age = max(ages.values())

        if skew <= HARD_SKEW_S:
            mode = "HARD"
            aligned = True
        elif skew <= SOFT_SKEW_S and max_age <= MAX_SOFT_AGE_S:
            mode = "SOFT"
            aligned = True
        else:
            mode = "UNALIGNED"
            aligned = False

        return {
            "aligned": aligned,
            "mode": mode,
            "skew_s": round(skew, 4),
            "max_age_s": round(max_age, 4),
            "ages_s": {k: round(v, 4) for k, v in ages.items()},
        }

    def analyze(state, side):
        report = dict(original_analyze(state, side) or {})
        fresh = report.get("freshness") or {}
        if fresh.get("mode") == "SOFT":
            lead = report.get("displacement_dominance")
            gap = _f(report.get("lead_gap_bps"))
            # Soft alignment needs a stronger separation before it can influence regime.
            if lead == "PERP_LED" and gap < 1.60:
                report["displacement_dominance"] = "BALANCED"
                report["soft_alignment_downgraded"] = True
            elif lead == "CASH_LED" and gap > -1.00:
                report["displacement_dominance"] = "BALANCED"
                report["soft_alignment_downgraded"] = True
        return report

    flow_lead_module._freshness = freshness
    flow_lead_module.analyze = analyze
    flow_lead_module._alignment_hooked = True
