"""Cash liquidity-response observation owner.

This module is deliberately authority-free.  It answers one question only:
what did cash liquidity do after an executed cash-flow event?  It does not own
Bias, Ignition, Entry, Action, Guardian, Risk, or execution.

RAW L2 removal/cancellation is ambiguous.  A caller must explicitly provide an
execution-linked depletion observation before this module may call something
conversion or absorption.  Missing linkage remains UNKNOWN rather than being
filled with a wall/cancel heuristic.
"""

VERSION = "CASH_LIQUIDITY_RESPONSE_V1_DATA_ONLY"
SEMANTIC_ROLE = "CASH_LIQUIDITY_RESPONSE_OBSERVATION_ONLY"
AUTHORITY = False
VALID_SIDES = frozenset(("LONG", "SHORT"))


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _base(observation):
    source = dict(observation or {})
    return {
        "version": VERSION,
        "semantic_role": SEMANTIC_ROLE,
        "authority": False,
        "source_id": source.get("source_id"),
        "venue": source.get("venue"),
        "epoch": source.get("epoch"),
        "event_time_ms": source.get("event_time_ms"),
        "receive_time_ms": source.get("receive_time_ms"),
        "side": str(source.get("side") or "").upper(),
        "state": "UNKNOWN",
        "reason": "INSUFFICIENT_CAUSAL_EVIDENCE",
        "can_create_direction": False,
    }


def classify(observation):
    """Classify an already execution-linked cash-liquidity observation.

    Expected fields are deliberately semantic rather than venue-specific:
    - side: LONG/SHORT, describing the executed taker-flow direction
    - executed_quote: executed cash notional in the same observation window
    - execution_linked: True only when depletion is correlated to executed trades
    - price_progress_bps: signed raw price move; positive means price rose
    - opposing_depletion_quote: liquidity removed from the side takers attack
    - opposing_refill_quote: replenishment on that same attacked side
    - supporting_retreat_quote: opposite-side liquidity retreat; context only

    No field here is a strategy threshold.  The module compares causal signs and
    relative book response only.  A future consumer may use the observation only
    after replay proves a named benefit and explicitly changes authority wiring.
    """
    source = dict(observation or {})
    out = _base(source)
    side = out["side"]
    if side not in VALID_SIDES:
        out["reason"] = "SIDE_UNKNOWN"
        return out

    if not bool(source.get("execution_linked", False)):
        out["reason"] = "L2_NOT_LINKED_TO_EXECUTION"
        return out

    executed_quote = max(0.0, _f(source.get("executed_quote")))
    depletion = max(0.0, _f(source.get("opposing_depletion_quote")))
    refill = max(0.0, _f(source.get("opposing_refill_quote")))
    retreat = max(0.0, _f(source.get("supporting_retreat_quote")))
    raw_progress = _f(source.get("price_progress_bps"))
    signed_progress = raw_progress if side == "LONG" else -raw_progress

    out["metrics"] = {
        "executed_quote": executed_quote,
        "signed_price_progress_bps": signed_progress,
        "opposing_depletion_quote": depletion,
        "opposing_refill_quote": refill,
        "supporting_retreat_quote": retreat,
    }
    if executed_quote <= 0.0:
        out["reason"] = "NO_EXECUTED_CASH_FLOW"
        return out

    # Executed takers consumed opposing liquidity and price accepted the move.
    if signed_progress > 0.0 and depletion > 0.0 and refill <= depletion:
        out.update(
            state="FLOW_CONVERTING",
            reason="EXECUTED_FLOW_DEPLETION_WITH_PRICE_ACCEPTANCE",
        )
        return out

    # Executed takers met equal-or-greater replenishment and failed to progress.
    if signed_progress <= 0.0 and refill >= depletion and refill > 0.0:
        out.update(
            state="ABSORBED",
            reason="EXECUTED_FLOW_MET_BY_REFILL_WITHOUT_PRICE_PROGRESS",
        )
        return out

    # Price progressed while the supporting side of the book retreated.  This
    # may amplify a move but is not execution and cannot prove informed demand.
    if signed_progress > 0.0 and retreat > 0.0 and depletion <= 0.0:
        out.update(
            state="LIQUIDITY_RETREAT",
            reason="PRICE_PROGRESS_WITH_SUPPORTING_BOOK_RETREAT_ONLY",
        )
        return out

    if refill > depletion and refill > 0.0:
        out.update(
            state="REFILLING",
            reason="ATTACKED_LIQUIDITY_REPLENISHING_FASTER_THAN_DEPLETION",
        )
        return out

    out["reason"] = "MIXED_OR_INCOMPLETE_LIQUIDITY_RESPONSE"
    return out


def unknown(reason="UNAVAILABLE"):
    """Explicit fail-closed observation for missing/stale/gapped source data."""
    out = _base({})
    out["reason"] = str(reason or "UNAVAILABLE")
    return out
