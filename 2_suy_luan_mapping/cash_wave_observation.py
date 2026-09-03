"""Authority-free executed-cash wave observation for Bias.

This module answers one narrow question:
WHAT IS THE CURRENT STATE OF THE INDEPENDENT CASH WAVE?

It does not own Bias direction, Entry timing, execution, Guardian, or Risk.
It deliberately ignores Futures/OI as directional evidence.  A wave is defined
by dual-cash executed flow and the price response that flow actually achieved,
not by a fixed elapsed window or a static volume/price profile.
"""

VERSION = "CASH_WAVE_OBSERVATION_V1"
AUTHORITY = False
VALID_SIDES = frozenset(("LONG", "SHORT"))


def _side(node):
    value = str((node or {}).get("vote") or "ABSTAIN").upper()
    return value if value in VALID_SIDES else "ABSTAIN"


def classify_segment(segment):
    """Classify one non-overlapping cash segment.

    `price` and `flow` must already be dual-independent-cash observations from
    the canonical Bias owner.  This function adds mechanism semantics only.
    """
    source = dict(segment or {})
    price_side = _side(source.get("price"))
    flow_side = _side(source.get("flow"))
    price_reason = str((source.get("price") or {}).get("reason") or "")
    flow_reason = str((source.get("flow") or {}).get("reason") or "")

    if "EPOCH_MISMATCH" in price_reason or "EPOCH" in flow_reason:
        state = "UNKNOWN"
        reason = "CAUSAL_EPOCH_UNCERTAIN"
        side = "ABSTAIN"
    elif price_side in VALID_SIDES and flow_side == price_side:
        state = "CONVERTING"
        reason = "DUAL_CASH_FLOW_CONVERTS_TO_PRICE"
        side = price_side
    elif price_side in VALID_SIDES and flow_side in VALID_SIDES and flow_side != price_side:
        state = "CONTRADICTED"
        reason = "DUAL_CASH_FLOW_PRICE_CONTRADICTION"
        side = "ABSTAIN"
    elif flow_side in VALID_SIDES and price_side == "ABSTAIN":
        state = "FLOW_NONCONVERSION"
        reason = "EXECUTED_CASH_WITHOUT_DUAL_CASH_PRICE_ACCEPTANCE"
        side = flow_side
    elif price_side in VALID_SIDES:
        state = "PRICE_ACCEPTED_FLOW_UNRESOLVED"
        reason = "DUAL_CASH_PRICE_WITHOUT_EXECUTED_FLOW_CORROBORATION"
        side = price_side
    else:
        state = "NEUTRAL_OR_UNKNOWN"
        reason = "NO_DIRECTIONAL_CASH_CONTROL_IN_SEGMENT"
        side = "ABSTAIN"

    return {
        "version": VERSION,
        "authority": False,
        "start_age_seconds": source.get("start_age_seconds"),
        "end_age_seconds": source.get("end_age_seconds"),
        "side": side,
        "price_side": price_side,
        "flow_side": flow_side,
        "state": state,
        "reason": reason,
        "price": source.get("price"),
        "flow": source.get("flow"),
    }


def _liquidity_state(liquidity, side):
    """Optional refinement only; liquidity can never create direction."""
    side = str(side or "ABSTAIN").upper()
    for row in (liquidity or ()):
        item = dict(row or {})
        if str(item.get("side") or "").upper() != side:
            continue
        state = str(item.get("state") or "UNKNOWN").upper()
        if state in {"ABSORBED", "REFILLING", "FLOW_CONVERTING", "LIQUIDITY_RETREAT"}:
            return state
    return "UNKNOWN"


def infer(segments, previous_side="ABSTAIN", liquidity=()):
    """Infer the current active cash-wave mechanism without a clock-time vote.

    Segments must be non-overlapping and ordered newest -> oldest. Historical
    segments are context/falsification evidence; the newest segment owns the
    question of what is happening *now*.
    """
    observations = [classify_segment(row) for row in list(segments or ())]
    previous = str(previous_side or "ABSTAIN").upper()
    if previous not in VALID_SIDES:
        previous = "ABSTAIN"

    if not observations:
        return {
            "version": VERSION, "authority": False,
            "raw_side": "ABSTAIN", "wave_state": "UNKNOWN",
            "phase": "WARMUP_OR_NEUTRAL", "context_side": previous,
            "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
            "meaningful_for_action": False, "falsifier": None,
            "segments": observations,
        }

    latest = observations[0]
    older_recent = observations[1:3]
    immediate_previous = older_recent[0] if older_recent else None
    latest_side = latest["side"]
    latest_state = latest["state"]

    if latest_state == "UNKNOWN":
        return {
            "version": VERSION, "authority": False,
            "raw_side": "ABSTAIN", "wave_state": "UNKNOWN",
            "phase": "SOURCE_OR_EPOCH_UNKNOWN", "context_side": previous,
            "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
            "meaningful_for_action": False, "falsifier": "CAUSAL_EPOCH_UNCERTAIN",
            "segments": observations,
        }

    if latest_state == "CONTRADICTED":
        return {
            "version": VERSION, "authority": False,
            "raw_side": "ABSTAIN", "wave_state": "CONTRADICTED",
            "phase": "CASH_EVIDENCE_CONTRADICTED", "context_side": previous,
            "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
            "meaningful_for_action": False, "falsifier": latest["reason"],
            "segments": observations,
        }

    if previous in VALID_SIDES:
        if latest_state == "CONVERTING" and latest_side == previous:
            return {
                "version": VERSION, "authority": False,
                "raw_side": previous, "wave_state": "CONTROLLED",
                "phase": "ESTABLISHED_TREND", "context_side": previous,
                "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
                "meaningful_for_action": True, "falsifier": None,
                "segments": observations,
            }

        if latest_state == "CONVERTING" and latest_side in VALID_SIDES and latest_side != previous:
            # Current control is determined by the nearest causal sequence, not
            # by a stale 180s/600s segment.  If the immediately preceding
            # segment already converted with the new side, an older old-side
            # segment is historical context, not a live veto.
            old_still_converts = bool(
                immediate_previous
                and immediate_previous["state"] == "CONVERTING"
                and immediate_previous["side"] == previous
            )
            new_recent_support = bool(
                immediate_previous
                and immediate_previous["state"] == "CONVERTING"
                and immediate_previous["side"] == latest_side
            )
            old_failure_seen = any(
                row["flow_side"] == previous
                and row["state"] in {"FLOW_NONCONVERSION", "CONTRADICTED"}
                for row in older_recent
            )
            transfer = bool(
                not old_still_converts
                and (new_recent_support or old_failure_seen or immediate_previous)
            )
            if transfer:
                return {
                    "version": VERSION, "authority": False,
                    "raw_side": latest_side, "wave_state": "CONTROL_TRANSFER",
                    "phase": "REVERSAL_CANDIDATE", "context_side": previous,
                    "candidate_side": latest_side, "control_transfer_confirmed": True,
                    "meaningful_for_action": True,
                    "falsifier": "OLD_SIDE_NO_LONGER_CONVERTS",
                    "segments": observations,
                }
            return {
                "version": VERSION, "authority": False,
                "raw_side": "ABSTAIN", "wave_state": "TRANSITION",
                "phase": "UNPROVEN_CONTROL_TRANSFER", "context_side": previous,
                "candidate_side": latest_side, "control_transfer_confirmed": False,
                "meaningful_for_action": False,
                "falsifier": "OLD_SIDE_FAILURE_NOT_OBSERVED",
                "segments": observations,
            }

        if latest["flow_side"] == previous and latest_state == "FLOW_NONCONVERSION":
            liquidity_state = _liquidity_state(liquidity, previous)
            wave_state = "ABSORPTION" if liquidity_state in {"ABSORBED", "REFILLING"} else "EXHAUSTION"
            return {
                "version": VERSION, "authority": False,
                "raw_side": "ABSTAIN", "wave_state": wave_state,
                "phase": "OLD_CONTROL_FAILED_TO_CONVERT", "context_side": previous,
                "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
                "meaningful_for_action": False,
                "falsifier": "EXECUTED_FLOW_STOPPED_CONVERTING",
                "liquidity_state": liquidity_state,
                "segments": observations,
            }

        if latest["price_side"] in VALID_SIDES and latest["price_side"] != previous:
            return {
                "version": VERSION, "authority": False,
                "raw_side": previous, "wave_state": "PULLBACK",
                "phase": "PULLBACK_AGAINST_CONTEXT", "context_side": previous,
                "candidate_side": latest["price_side"],
                "control_transfer_confirmed": False,
                "meaningful_for_action": True, "falsifier": None,
                "segments": observations,
            }

        recent_old_conversion = any(
            row["state"] == "CONVERTING" and row["side"] == previous
            for row in observations[:2]
        )
        if not recent_old_conversion:
            return {
                "version": VERSION, "authority": False,
                "raw_side": "ABSTAIN", "wave_state": "EXHAUSTION",
                "phase": "CONTROL_EXHAUSTED", "context_side": previous,
                "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
                "meaningful_for_action": False,
                "falsifier": "NO_RECENT_OLD_SIDE_CONVERSION",
                "segments": observations,
            }

        return {
            "version": VERSION, "authority": False,
            "raw_side": previous, "wave_state": "PULLBACK",
            "phase": "PULLBACK_AGAINST_CONTEXT", "context_side": previous,
            "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
            "meaningful_for_action": True, "falsifier": None,
            "segments": observations,
        }

    if latest_state == "CONVERTING" and latest_side in VALID_SIDES:
        persistent = bool(
            immediate_previous
            and immediate_previous["state"] == "CONVERTING"
            and immediate_previous["side"] == latest_side
        )
        return {
            "version": VERSION, "authority": False,
            "raw_side": latest_side,
            "wave_state": "CONTROLLED" if persistent else "EMERGING_CONTROL",
            "phase": "ESTABLISHED_TREND" if persistent else "CONTEXT_WITHOUT_CONFIRMATION",
            "context_side": latest_side,
            "candidate_side": "ABSTAIN",
            "control_transfer_confirmed": False,
            "meaningful_for_action": persistent,
            "falsifier": None,
            "segments": observations,
        }

    return {
        "version": VERSION, "authority": False,
        "raw_side": "ABSTAIN", "wave_state": "UNKNOWN",
        "phase": "WARMUP_OR_NEUTRAL", "context_side": "ABSTAIN",
        "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
        "meaningful_for_action": False, "falsifier": None,
        "segments": observations,
    }
