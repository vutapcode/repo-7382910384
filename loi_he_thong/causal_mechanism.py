"""Canonical causal mechanism classification for the active market move.

This module answers one question only: what mechanism is producing the
observed move? It classifies POSITION_BUILD, UNWIND, FORCED_CLOSING,
CASH_CONTROL_AFTER_UNWIND, or UNRESOLVED.

It never creates LONG/SHORT direction. OI explains opening vs closing,
not direction. forceOrder never creates direction. Futures/perpetual flow
never substitutes for missing independent cash.
"""

VERSION = "CAUSAL_MECHANISM_V1"

def classify(oi_verification, liquidation_snapshot, cash_conversion_evidence):
    """Classify the causal mechanism driving the move."""
    oi = dict(oi_verification or {})
    liq = dict(liquidation_snapshot or {})
    cash = dict(cash_conversion_evidence or {})

    status = str(oi.get("status") or "UNAVAILABLE").upper()
    intent = str(oi.get("intent") or "UNKNOWN").upper()
    
    burst = bool(liq.get("burst"))
    decelerating = bool(liq.get("decelerating"))
    forced = burst or decelerating

    dual_cash = bool(cash.get("dual_cash_independent"))

    if not status.startswith("FRESH_"):
        return "UNRESOLVED"

    if status == "FRESH_POSITION_BUILD":
        return "POSITION_BUILD"

    if status == "FRESH_UNWIND":
        if not forced:
            return "UNWIND"
        if decelerating and dual_cash:
            return "CASH_CONTROL_AFTER_UNWIND"
        return "FORCED_CLOSING"
        
    return "UNRESOLVED"
