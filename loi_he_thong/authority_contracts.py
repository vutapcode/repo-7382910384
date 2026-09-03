"""Serialization envelope for the four active authority owners.

This module owns no market or trading conclusion.  It only seals a snapshot
created by an existing owner and verifies that downstream code did not rewrite
that snapshot.  Contracts stay JSON-native so the active journal can persist
them without a custom encoder.
"""

import hashlib
import json
import math


VERSION = "FOUR_AUTHORITY_CONTRACTS_V1"
ENTRY_HANDOFF_VERSION = "ENTRY_THESIS_HANDOFF_V1"
LAYERS = {"MARKET_TRUTH", "ACTION", "EXECUTION", "SAFETY"}
ENTRY_ACTIONS = {"ACT_TAKER_NOW", "POST_MAKER"}


def _plain(value):
    if isinstance(value, dict):
        return {
            str(key): _plain(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, set):
        return sorted((_plain(item) for item in value), key=repr)
    if isinstance(value, float) and not math.isfinite(value):
        # Telemetry sealing must never interrupt an otherwise valid strategy
        # decision. Preserve the measurement defect explicitly instead of
        # asking JSON encoders to accept NaN/Infinity.
        return "NON_FINITE_MEASUREMENT"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _digest(payload):
    encoded = json.dumps(
        _plain(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal(layer, owner, causal_episode_id, payload):
    """Return a content-addressed JSON snapshot from one named owner."""
    layer = str(layer or "").upper()
    if layer not in LAYERS:
        raise ValueError("UNKNOWN_AUTHORITY_LAYER")
    owner = str(owner or "").upper()
    if not owner:
        raise ValueError("AUTHORITY_OWNER_MISSING")
    episode_id = str(causal_episode_id or "")
    body = {
        "contract_schema": VERSION,
        "layer": layer,
        "owner": owner,
        "causal_episode_id": episode_id or None,
        "immutable_snapshot": True,
        **_plain(dict(payload or {})),
    }
    body.pop("contract_hash", None)
    return {**body, "contract_hash": _digest(body)}


def verify(contract):
    contract = _plain(dict(contract or {}))
    supplied = str(contract.pop("contract_hash", "") or "")
    return bool(
        supplied
        and contract.get("contract_schema") == VERSION
        and contract.get("layer") in LAYERS
        and supplied == _digest(contract)
    )


def bundle(*contracts):
    """Seal four owner snapshots that refer to the same causal episode."""
    by_layer = {}
    episode_ids = set()
    for contract in contracts:
        row = _plain(dict(contract or {}))
        if not verify(row):
            raise ValueError("INVALID_AUTHORITY_CONTRACT")
        layer = str(row.get("layer") or "")
        if layer in by_layer:
            raise ValueError("DUPLICATE_AUTHORITY_LAYER")
        by_layer[layer] = row
        episode_id = str(row.get("causal_episode_id") or "")
        if episode_id:
            episode_ids.add(episode_id)
    if set(by_layer) != LAYERS:
        raise ValueError("FOUR_AUTHORITY_LAYERS_REQUIRED")
    if len(episode_ids) > 1:
        raise ValueError("CAUSAL_EPISODE_ID_MISMATCH")
    body = {
        "version": VERSION,
        "causal_episode_id": next(iter(episode_ids), None),
        "contracts": by_layer,
        "immutable_snapshot": True,
    }
    return {**body, "bundle_hash": _digest(body)}


def verify_bundle(value):
    value = _plain(dict(value or {}))
    supplied = str(value.pop("bundle_hash", "") or "")
    contracts = dict(value.get("contracts") or {})
    return bool(
        supplied
        and value.get("version") == VERSION
        and set(contracts) == LAYERS
        and all(verify(contract) for contract in contracts.values())
        and supplied == _digest(value)
    )


def freeze_entry_handoff(value, *, expected_side=None, expected_episode_id=None):
    """Freeze the exact Truth and Action contracts that approved an Entry.

    This is a transfer envelope, not a fifth authority.  Execution may replace
    its own contract later without changing either hash captured here.
    """
    value = _plain(dict(value or {}))
    if not verify_bundle(value):
        raise ValueError("AUTHORITY_BUNDLE_INVALID")
    contracts = dict(value.get("contracts") or {})
    truth = dict(contracts.get("MARKET_TRUTH") or {})
    action = dict(contracts.get("ACTION") or {})
    episode_id = str(value.get("causal_episode_id") or "")
    side = str(truth.get("side") or "ABSTAIN").upper()
    if not episode_id:
        raise ValueError("ENTRY_HANDOFF_EPISODE_MISSING")
    if str(action.get("action") or "") not in ENTRY_ACTIONS:
        raise ValueError("ACTION_NOT_ENTRY_APPROVED")
    if truth.get("status") != "SUPPORTED":
        raise ValueError("MARKET_TRUTH_NOT_SUPPORTED")
    if side not in {"LONG", "SHORT"}:
        raise ValueError("MARKET_TRUTH_SIDE_INVALID")
    if expected_side is not None and side != str(expected_side).upper():
        raise ValueError("ENTRY_HANDOFF_SIDE_MISMATCH")
    if expected_episode_id is not None and episode_id != str(
        expected_episode_id or ""
    ):
        raise ValueError("ENTRY_HANDOFF_EPISODE_MISMATCH")
    body = {
        "version": ENTRY_HANDOFF_VERSION,
        "immutable_snapshot": True,
        "causal_episode_id": episode_id or None,
        "side": side,
        "thesis_version": truth.get("version"),
        "mechanism": truth.get("mechanism"),
        "supporting_evidence": list(truth.get("supporting_evidence") or ()),
        "competing_explanations": list(
            truth.get("competing_explanations") or ()
        ),
        "falsifiers": list(truth.get("falsifiers") or ()),
        "expected_next_observations": list(
            truth.get("expected_next_observations") or ()
        ),
        "source_health": dict(truth.get("source_health") or {}),
        "market_truth_hash": truth.get("contract_hash"),
        "action_hash": action.get("contract_hash"),
        "market_thesis": truth,
        "action_contract": action,
    }
    return {**body, "handoff_hash": _digest(body)}


def verify_entry_handoff(value, *, expected_side=None, expected_episode_id=None):
    value = _plain(dict(value or {}))
    supplied = str(value.pop("handoff_hash", "") or "")
    truth = dict(value.get("market_thesis") or {})
    action = dict(value.get("action_contract") or {})
    side = str(value.get("side") or "ABSTAIN").upper()
    episode_id = str(value.get("causal_episode_id") or "")
    if not (
        supplied
        and value.get("version") == ENTRY_HANDOFF_VERSION
        and supplied == _digest(value)
        and verify(truth)
        and verify(action)
        and truth.get("layer") == "MARKET_TRUTH"
        and action.get("layer") == "ACTION"
        and truth.get("status") == "SUPPORTED"
        and str(action.get("action") or "") in ENTRY_ACTIONS
        and bool(episode_id)
        and truth.get("contract_hash") == value.get("market_truth_hash")
        and action.get("contract_hash") == value.get("action_hash")
        and str(truth.get("side") or "").upper() == side
        and str(truth.get("causal_episode_id") or "") == episode_id
        and str(action.get("causal_episode_id") or "") == episode_id
    ):
        return False
    if expected_side is not None and side != str(expected_side).upper():
        return False
    if expected_episode_id is not None and episode_id != str(
        expected_episode_id or ""
    ):
        return False
    return True


def read_journal_bundle(payload):
    """Read new bundles; expose old journal fields as non-authoritative only."""
    payload = dict(payload or {})
    direct = payload.get("authority_contracts")
    if direct is None:
        direct = (payload.get("decision_record") or {}).get(
            "authority_contracts"
        )
    if isinstance(direct, dict):
        return {
            "version": VERSION,
            "valid": verify_bundle(direct),
            "authority_eligible": verify_bundle(direct),
            "compatibility_only": False,
            "bundle": _plain(direct),
        }

    legacy_fields = []
    for name in (
        "decision", "entry_authority_decision", "execution_policy",
        "entry_causal_thesis", "guardian_state", "risk_state",
    ):
        if name in payload:
            legacy_fields.append(name)
    if (payload.get("decision_record") or {}).get("output"):
        legacy_fields.append("decision_record.output")
    return {
        "version": "LEGACY_AUTHORITY_FIELDS_READ_ONLY",
        "valid": False,
        "authority_eligible": False,
        "compatibility_only": True,
        "bundle": None,
        "legacy_fields_present": legacy_fields,
    }
