"""WStrade Evidence Stack V2 — research-only reference.

Uses feeds already present in recorder. AUTHORITY=False.
No GO/VETO, no thresholds, no live imports.
"""

AUTHORITY = False
EPS = 1e-12


def ratio(a, b):
    return None if abs(float(b)) <= EPS else float(a) / float(b)


def flow_efficiency(*, side, buy_quote, sell_quote, first_price, last_price):
    """Executed-flow conversion. Falling efficiency => exhaustion/absorption candidate."""
    side = str(side).upper()
    directional = float(buy_quote if side == "LONG" else sell_quote)
    if directional <= 0 or first_price <= 0 or last_price <= 0:
        return {"efficiency": None, "authority": False}
    raw = (float(last_price) / float(first_price) - 1.0) * 10000.0
    progress = raw if side == "LONG" else -raw
    return {
        "progress_bps": progress,
        "directional_quote": directional,
        "efficiency_bps_per_million": progress / (directional / 1_000_000.0),
        "authority": False,
    }


def liquidation_phase(*, short_pressure, long_pressure, oi_intent, cash_acceptance,
                      efficiency_improving):
    """forceOrder is sampled evidence only; never subtract it from aggTrade."""
    oi = str(oi_intent).upper()
    cash = str(cash_acceptance).upper()
    if short_pressure > long_pressure and oi == "UNWIND":
        return "CASCADE_OR_BUILDUP"
    if short_pressure < long_pressure and oi == "UNWIND" and cash in {"WEAK", "OPPOSING"}             and efficiency_improving is False:
        return "TAIL_CANDIDATE"
    if cash == "CONVERTING" and efficiency_improving is True:
        return "CONTINUATION_AFTER_LIQUIDATION"
    return "UNKNOWN"


def oi_efficiency(*, oi_intent, oi_delta_pct, directional_price_bps):
    """OI confirms state; it never supplies trade direction."""
    intent = str(oi_intent).upper()
    p = None if directional_price_bps is None else float(directional_price_bps)
    if intent == "POSITION_BUILD":
        state = "BUILD_CONVERTING" if p and p > 0 else "BUILD_STALLED_OR_OPPOSING"
    elif intent == "UNWIND":
        state = "UNWIND_CONVERTING" if p and p > 0 else "UNWIND_STALLED_OR_REJECTED"
    else:
        state = "OI_NEUTRAL_OR_UNKNOWN"
    return {
        "state": state,
        "bps_per_abs_oi_pct": None if not oi_delta_pct or p is None else p / abs(float(oi_delta_pct)),
        "authority": False,
    }


def liquidity_response(*, depletion_quote, refill_quote, refill_ms):
    """Static wall/OBI alone is never absorption; require executed depletion + refill."""
    depletion = max(0.0, float(depletion_quote))
    refill = max(0.0, float(refill_quote))
    return {
        "refill_ratio": ratio(refill, depletion),
        "refill_ms": None if refill_ms is None else float(refill_ms),
        "has_executed_depletion": depletion > 0.0,
        "authority": False,
    }


def thesis_recovery(*, efficiency_change, refill_change, price_progress_bps,
                    oi_intent, opposing_cash):
    """Guardian telemetry only. Replay/ablation must prove any future live rule."""
    recover = int(efficiency_change > 0) + int(refill_change > 0) + int(price_progress_bps > 0)
    deteriorate = int(efficiency_change < 0) + int(refill_change < 0) + int(price_progress_bps < 0)
    recover += int(str(oi_intent).upper() == "UNWIND")
    deteriorate += int(str(oi_intent).upper() == "POSITION_BUILD") + int(bool(opposing_cash))
    if recover > deteriorate:
        label = "RECOVERY_EVIDENCE_DOMINANT"
    elif deteriorate > recover:
        label = "DETERIORATION_EVIDENCE_DOMINANT"
    else:
        label = "CONFLICTED_OR_INCOMPLETE"
    return {"label": label, "authority": False}


def shared_wave_consumed(previous_max, current_measurement):
    """Same causal wave may mature; it must never become young again."""
    return max(float(previous_max), float(current_measurement))
