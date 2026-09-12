"""Read-only forensic projection of one already-made runtime decision.

This module never votes, gates, or mutates strategy state. It only makes the
existing evidence, epistemic status, blocker ownership, and causal lineage
explicit enough for deterministic investigation.
"""

VERSION = "FORENSIC_DECISION_DOSSIER_V1"
AUTHORITY = False

_UNKNOWN_MARKERS = (
    "NOT_READY", "WAIT_", "STALE", "UNKNOWN", "UNVERIFIED", "MISSING",
    "ABSENT", "UNOBSERVED", "INSUFFICIENT", "NOT_OBSERVED",
)
_FALSIFIED_MARKERS = (
    "FAIL", "FALSIF", "INVALID", "EXHAUST", "OPPOSITE", "CONTRADICTION",
    "EXPIRED", "DECAY", "REJECT", "NONCONVERSION", "ABSORBED",
)


def epistemic_status(*, allowed=None, reason=None):
    """Separate knowledge state from the final WAIT/GO action."""
    if allowed is True:
        return "SUPPORTED"
    text = str(reason or "").upper()
    if any(marker in text for marker in _UNKNOWN_MARKERS):
        return "UNKNOWN"
    if any(marker in text for marker in _FALSIFIED_MARKERS):
        return "FALSIFIED"
    return "UNKNOWN" if allowed is not True else "SUPPORTED"


def _dict(value):
    return value if isinstance(value, dict) else {}


def _collect_refs(value, path="", out=None):
    out = [] if out is None else out
    if len(out) >= 128:
        return out
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            name = str(key).lower()
            if (
                isinstance(item, (str, int)) and item not in ("", 0)
                and (
                    name.endswith("event_id") or name.endswith("evidence_id")
                    or name.endswith("proof_hash") or name.endswith("contract_hash")
                    or name in {"bundle_hash", "handoff_hash"}
                )
            ):
                out.append({"kind": name, "ref": str(item), "source": child})
            elif isinstance(item, (dict, list, tuple)):
                _collect_refs(item, child, out)
            if len(out) >= 128:
                break
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _collect_refs(item, f"{path}[{index}]", out)
            if len(out) >= 128:
                break
    return out


def _owner_for_reason(reason, primary_owner=None):
    text = str(reason or "").upper()
    if primary_owner:
        return str(primary_owner).upper(), "EXPLICIT_GATE_OUTCOME"
    if any(marker in text for marker in ("COST", "EDGE", "ECONOMIC", "ALPHA")):
        return "ECONOMICS", "TAXONOMY_INFERRED"
    if any(marker in text for marker in ("SAFETY", "RISK", "CPU", "HEALTH")):
        return "SAFETY", "TAXONOMY_INFERRED"
    if any(marker in text for marker in ("EXECUTION", "SUBMIT", "FILL", "BBO")):
        return "EXECUTION", "TAXONOMY_INFERRED"
    if any(marker in text for marker in (
        "BIAS", "OPPOSITE", "CONTRADICTION", "ABSOR", "NONCONVERSION",
        "MARKET_TRUTH",
    )):
        return "MARKET_TRUTH", "TAXONOMY_INFERRED"
    if any(marker in text for marker in (
        "TIMING", "STALE", "CLOCK", "EPOCH", "CHASE", "LATE", "DECAY",
    )):
        return "TIMING", "TAXONOMY_INFERRED"
    return "UNATTRIBUTED", "UNRESOLVED"


def _question(question_id, owner, status, *, reason=None, can_block=False,
              evidence_refs=None, detail=None):
    return {
        "question_id": str(question_id),
        "owner": str(owner or "UNATTRIBUTED").upper(),
        "epistemic_status": str(status).upper(),
        "reason": reason,
        "gate_effect": "BLOCK" if can_block else "NON_BLOCKING",
        "evidence_refs": list(evidence_refs or ()),
        "detail": detail,
    }


def build_decision_dossier(snapshot):
    """Project one immutable decision snapshot without re-adjudicating it."""
    snapshot = _dict(snapshot)
    inputs = _dict(snapshot.get("inputs"))
    output = _dict(snapshot.get("output"))
    gate = _dict(output.get("entry_gate_outcome"))
    bundle = _dict(snapshot.get("authority_contracts"))
    contracts = _dict(bundle.get("contracts"))
    cost = _dict(output.get("cost"))
    refs = _collect_refs({"inputs": inputs, "authority_contracts": bundle})

    questions = []
    truth = _dict(contracts.get("MARKET_TRUTH"))
    truth_status = str(truth.get("status") or "UNKNOWN").upper()
    questions.append(_question(
        "market_truth_supported", "MARKET_TRUTH",
        "SUPPORTED" if truth_status == "SUPPORTED" else epistemic_status(
            allowed=False, reason=output.get("blocking_reason") or truth_status,
        ),
        reason=truth_status, can_block=str(gate.get("owner")).upper() == "MARKET_TRUTH",
        evidence_refs=[row["ref"] for row in refs if "market" in row["source"]][:32],
    ))

    timing_owner = str(gate.get("owner") or "").upper() == "TIMING"
    questions.append(_question(
        "timing_attempt_executable_now", "TIMING",
        epistemic_status(
            allowed=True if gate.get("allowed") and timing_owner else None,
            reason=gate.get("reason") if timing_owner else "NOT_APPLICABLE",
        ) if timing_owner else "NOT_APPLICABLE",
        reason=gate.get("reason") if timing_owner else None,
        can_block=timing_owner and not bool(gate.get("allowed")),
    ))

    cost_ok = cost.get("cost_ok")
    questions.append(_question(
        "guardian_net_edge_clears_frozen_cost", "ECONOMICS",
        "SUPPORTED" if cost_ok is True else "FALSIFIED" if cost_ok is False else "UNKNOWN",
        reason=("COST_OK" if cost_ok is True else "EDGE_COST_FAIL" if cost_ok is False
                else "ECONOMIC_EDGE_UNKNOWN"),
        can_block=str(gate.get("owner") or "").upper() == "ECONOMICS",
        detail={
            key: cost.get(key) for key in (
                "expected_net_bps_model", "minimum_net_edge_bps",
                "execution_style", "commission_verified",
            )
        },
    ))

    for name, row in _dict(
        _dict(inputs.get("distance_to_boundary")).get("boundaries")
    ).items():
        row = _dict(row)
        observed = bool(row.get("observed"))
        distance = row.get("distance_to_boundary")
        status = "UNKNOWN"
        if observed and distance is not None:
            try:
                status = (
                    "SUPPORTED" if float(distance) >= 0.0 else "FALSIFIED"
                )
            except (TypeError, ValueError):
                status = "UNKNOWN"
        questions.append(_question(
            f"boundary:{name}", row.get("owner"), status,
            reason=row.get("question"), can_block=False,
            detail={
                key: row.get(key) for key in (
                    "threshold_id", "observed_value", "threshold",
                    "distance_to_boundary", "passing_relation", "source",
                )
            },
        ))

    failed = list(output.get("blocking_reasons") or ())
    primary = str(output.get("blocking_reason") or "")
    blockers = []
    for reason in failed or ([primary] if primary else []):
        explicit = str(gate.get("owner") or "") if str(reason) == primary else None
        owner, owner_source = _owner_for_reason(reason, explicit)
        blockers.append({
            "blocker_id": str(reason), "owner": owner,
            "owner_source": owner_source, "can_block": True,
            "epistemic_status": epistemic_status(allowed=False, reason=reason),
            "stage": gate.get("stage") if str(reason) == primary else None,
            "evidence_refs": [row["ref"] for row in refs[:32]],
        })

    decision = str(output.get("decision") or "WAIT").upper()
    if decision == "WAIT" and not blockers:
        owner, owner_source = _owner_for_reason(
            output.get("reason"), gate.get("owner"),
        )
        blockers.append({
            "blocker_id": str(output.get("reason") or "UNATTRIBUTED_WAIT"),
            "owner": owner, "owner_source": owner_source, "can_block": True,
            "epistemic_status": "UNKNOWN", "stage": gate.get("stage"),
            "evidence_refs": [row["ref"] for row in refs[:32]],
        })

    ignored = [{
        "evidence": str(reason),
        "disposition": "DIAGNOSTIC_NON_BLOCKING",
        "reason": "NOT_AUTHORIZED_TO_VETO_CURRENT_DECISION",
    } for reason in output.get("diagnostic_reasons") or ()]

    return {
        "version": VERSION,
        "advisory_only": True,
        "lineage": {
            "market_wave_id": snapshot.get("market_wave_id")
            or snapshot.get("causal_episode_id"),
            "causal_episode_id": snapshot.get("causal_episode_id"),
            "timing_attempt_id": snapshot.get("timing_attempt_id"),
            "opportunity_id": snapshot.get("economic_opportunity_id"),
            "economic_opportunity_id": snapshot.get("economic_opportunity_id"),
            "decision_cycle_id": snapshot.get("cycle_id"),
            "order_id": snapshot.get("order_id"),
            "fill_id": snapshot.get("fill_id"),
            "position_id": snapshot.get("position_cycle_id"),
        },
        "market_evidence": {
            "evidence_refs": refs,
            "decision_time_ms": snapshot.get("decision_time_ms"),
            "raw_archive_status": "POINTER_NOT_YET_AVAILABLE",
        },
        "data_quality": {
            "open_interest": inputs.get("open_interest"),
            "coinbase": inputs.get("coinbase"),
            "clock_quality": _dict(inputs.get("ignition")).get("clock_quality"),
            "source_health": truth.get("source_health"),
        },
        "observation": {
            key: inputs.get(key) for key in (
                "bias", "s1_price_quorum", "s2_executed_flow_quorum",
                "s3_causal_validator", "flow_persistence", "price_impact",
                "oi_intent", "cash_perp_handoff", "price_acceptance",
                "regime", "spot_perp_relation",
            )
        },
        "question_results": questions,
        "authority": {
            "contract_model": bundle.get("version"),
            "bundle_hash": bundle.get("bundle_hash"),
            "layers": {
                name: {
                    "owner": _dict(contract).get("owner"),
                    "contract_hash": _dict(contract).get("contract_hash"),
                    "status": _dict(contract).get("status"),
                    "action": _dict(contract).get("action"),
                }
                for name, contract in contracts.items()
            },
            "blocking_gate_owner": gate.get("owner"),
            "blocking_gate_stage": gate.get("stage"),
        },
        "blockers": blockers,
        "unknowns": [
            row["question_id"] for row in questions
            if row["epistemic_status"] == "UNKNOWN"
        ],
        "falsified": [
            row["question_id"] for row in questions
            if row["epistemic_status"] == "FALSIFIED"
        ],
        "ignored_evidence": ignored,
        "decision": {
            "action": decision,
            "reason": output.get("reason"),
            "authorization_status": output.get("authorization_status"),
        },
        "counterfactual": snapshot.get("counterfactual"),
    }
