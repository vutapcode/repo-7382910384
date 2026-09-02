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
LAYERS = {"MARKET_TRUTH", "ACTION", "EXECUTION", "SAFETY"}


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
