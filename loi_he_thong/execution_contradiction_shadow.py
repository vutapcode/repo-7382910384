"""Phase-6 contradiction-only Execution shadow comparator.

Preparation only: no active submit path imports this module. It classifies
facts that Execution may own and compares them with the current active
revalidation verdict without changing side or order flow.
"""
from __future__ import annotations

import hashlib
import json

VERSION = "EXECUTION_CONTRADICTION_SHADOW_P6_V1"
AUTHORITY = False

FAIL_CLOSED_FACTS = (
    "reservation_ok",
    "opportunity_identity_ok",
    "causal_episode_identity_ok",
    "sealed_handoff_ok",
    "epoch_ok",
    "gap_free",
    "feed_fresh",
    "bbo_valid",
    "bbo_fresh",
    "order_filter_fill_feasible",
    "hard_risk_admitted",
)

STRATEGY_REJUDGMENT_REASONS = frozenset({
    "BIAS_SIDE_CHANGED",
    "BIAS_CONFIDENCE_DROPPED",
    "BIAS_STALE",
    "TRANSITION_AUTHORITY_DEPENDENCY_INVALID",
    "CURRENT_PHASE_SCALE_UNAVAILABLE",
    "CURRENT_IMPULSE_ALREADY_CONSUMED",
    "FOLLOWER_REQUIRED_AGAIN",
    "OLD_SIDE_FAILURE_REINTERPRETED",
})


def _digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def contradiction_only(facts, *, side):
    facts = dict(facts or {})
    side = str(side or "").upper()
    if side not in {"LONG", "SHORT"}:
        return {"ok": False, "reason": "SIDE_INVALID", "side": side}

    for name in FAIL_CLOSED_FACTS:
        if facts.get(name) is not True:
            return {"ok": False, "reason": f"EXECUTION_{name.upper()}_FAILED", "side": side}

    contradiction = dict(facts.get("post_go_contradiction") or {})
    venues = set(contradiction.get("venues") or ())
    kind = str(contradiction.get("kind") or "").upper()
    if kind == "CASH_PRICE_FLOW_REVERSAL" and venues & {"binance_spot", "coinbase_spot"}:
        return {"ok": False, "reason": "POST_GO_CASH_CONTRADICTION", "side": side}
    if kind == "OPPOSING_FLOW" and venues <= {"futures"}:
        return {"ok": True, "reason": "FUTURES_ONLY_OPPOSITION_CONTEXT", "side": side}
    if contradiction.get("market_truth_hash_mismatch") is True:
        return {"ok": False, "reason": "SEALED_MARKET_TRUTH_HASH_MISMATCH", "side": side}

    return {"ok": True, "reason": "CONTRADICTION_ONLY_PASS", "side": side}


def compare(active_ok, active_reason, facts, *, side):
    shadow = contradiction_only(facts, side=side)
    active_reason = str(active_reason or "UNKNOWN").upper()
    first_diff = None
    if bool(active_ok) != bool(shadow["ok"]) or (
        not active_ok and active_reason != shadow["reason"]
    ):
        first_diff = {
            "active_ok": bool(active_ok),
            "active_reason": active_reason,
            "shadow_ok": bool(shadow["ok"]),
            "shadow_reason": shadow["reason"],
            "active_reason_owner": (
                "STRATEGY_REJUDGMENT"
                if active_reason in STRATEGY_REJUDGMENT_REASONS
                else "EXECUTION_OR_UNKNOWN"
            ),
        }
    body = {
        "version": VERSION,
        "authority": AUTHORITY,
        "side": str(side or "").upper(),
        "active": {"ok": bool(active_ok), "reason": active_reason},
        "shadow": shadow,
        "first_differing_reason": first_diff,
        "policy": "SHADOW_ONLY_ACTIVE_ORDER_PATH_UNCHANGED",
    }
    return {**body, "comparison_hash": _digest(body)}
