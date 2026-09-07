"""Canonical causal mechanism classification for the active market move.

This module answers one question only: what mechanism is producing the
observed move? It classifies POSITION_BUILD, UNWIND, FORCED_CLOSING,
CASH_CONTROL_AFTER_UNWIND, or UNRESOLVED.

It never creates LONG/SHORT direction. OI explains opening vs closing,
not direction. forceOrder never creates direction. Futures/perpetual flow
never substitutes for missing independent cash.
"""

VERSION = "CAUSAL_MECHANISM_V1"


def _verified_dual_cash_conversion(cash):
    """Require present same-side flow->price conversion on both cash roots."""
    cash = dict(cash or {})
    side = str(cash.get("side") or "ABSTAIN").upper()
    venues = dict(cash.get("venues") or {})
    required = {"binance_spot", "coinbase_spot"}
    if (
        side not in {"LONG", "SHORT"}
        or not bool(cash.get("dual_cash_independent"))
        or not bool(cash.get("dual_cash_flow_price_conversion"))
        or not bool(cash.get("fresh"))
        or set(venues) != required
    ):
        return False
    max_age_ms = int(cash.get("max_age_ms", 600) or 600)
    return all(
        0 <= int((venues[name] or {}).get("age_ms", max_age_ms + 1))
            <= max_age_ms
        and float((venues[name] or {}).get("price_conversion_bps", 0.0) or 0.0)
            > 0.0
        for name in required
    )

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

    continuing_cash_control = _verified_dual_cash_conversion(cash)

    if not status.startswith("FRESH_"):
        return "UNRESOLVED"

    if status == "FRESH_POSITION_BUILD":
        return "POSITION_BUILD"

    if status == "FRESH_UNWIND":
        if not forced:
            return "UNWIND"
        if decelerating and continuing_cash_control:
            return "CASH_CONTROL_AFTER_UNWIND"
        return "FORCED_CLOSING"
        
    return "UNRESOLVED"
