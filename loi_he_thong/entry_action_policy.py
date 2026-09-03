"""Phase-6 Action Policy mirror contract.

Preparation only. This module has no runtime authority and introduces no
numeric strategy thresholds. It mirrors the launcher's existing GO +
execution_policy mapping and separately records explicit economics/expiry
counterfactual labels for offline comparison.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

VERSION = "ENTRY_ACTION_POLICY_P6_SHADOW_V1"
AUTHORITY = False

ACTIONS = frozenset({
    "ACT_TAKER_NOW",
    "POST_MAKER",
    "WAIT_INFORMATION",
    "ABANDON_ECONOMICS",
    "ABANDON_OPPORTUNITY_EXPIRED",
})


def _digest(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mirror_active_launcher(active_result, quorum_ok):
    """Mirror current launcher semantics; no new policy judgment."""
    result = dict(active_result or {})
    authorized = bool(result.get("decision") == "GO" and quorum_ok)
    execution_policy = str(result.get("execution_policy") or "").upper()
    if authorized and execution_policy == "TAKER":
        return "ACT_TAKER_NOW"
    if authorized and execution_policy == "MAKER":
        return "POST_MAKER"
    return "WAIT_INFORMATION"


def _explicit_counterfactual(active_result, economics, urgency_evidence, quorum_ok):
    """Classify only explicit pre-existing facts; never infer a new threshold."""
    result = dict(active_result or {})
    economics = dict(economics or {})
    urgency = dict(urgency_evidence or {})

    if urgency.get("opportunity_expired") is True:
        return "ABANDON_OPPORTUNITY_EXPIRED"
    if (
        economics.get("cost_ok") is False
        or economics.get("economic_viable") is False
        or result.get("economics_abandoned") is True
    ):
        return "ABANDON_ECONOMICS"
    return mirror_active_launcher(result, quorum_ok)


def evaluate(
    market_truth,
    economics,
    urgency_evidence,
    *,
    active_result,
    quorum_ok,
):
    """Return a non-authoritative mirror + counterfactual observation.

    `action` is intentionally the exact active-launcher mirror. The
    `counterfactual_action` is observation-only until Phase-4/5 evidence gates
    are complete.
    """
    truth = deepcopy(dict(market_truth or {}))
    active = deepcopy(dict(active_result or {}))
    economics = deepcopy(dict(economics or {}))
    urgency = deepcopy(dict(urgency_evidence or {}))

    action = mirror_active_launcher(active, quorum_ok)
    counterfactual = _explicit_counterfactual(
        active, economics, urgency, quorum_ok
    )
    if action not in ACTIONS or counterfactual not in ACTIONS:
        raise ValueError("PHASE6_ACTION_ENUM_INVALID")

    body = {
        "version": VERSION,
        "authority": AUTHORITY,
        "market_truth_hash": truth.get("contract_hash"),
        "causal_episode_id": truth.get("causal_episode_id"),
        "active_decision": active.get("decision"),
        "active_execution_policy": active.get("execution_policy"),
        "quorum_ok": bool(quorum_ok),
        "action": action,
        "counterfactual_action": counterfactual,
        "economics": economics,
        "urgency_evidence": urgency,
        "policy": "MIRROR_ONLY_NO_GO_WAIT_OR_EXECUTION_CHANGE",
    }
    return {**body, "decision_hash": _digest(body)}
