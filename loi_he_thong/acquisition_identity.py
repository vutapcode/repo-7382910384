"""Canonical validation for one sealed Bias cash-acquisition identity.

Bias creates this evidence, Ignition times it, and Market Thesis may freeze it
as a position root.  Keeping validation here prevents those owners from using
slightly different definitions of the same immutable acquisition.
"""

import hashlib
import json


HANDOFF_VERSION = "CASH_CONTROL_ACQUISITION_HANDOFF_V1"
SEED_VERSION = "POSITION_THESIS_SEED_V1"
IDENTITY_KIND = "BIAS_CASH_ACQUISITION"
REQUIRED_ROOTS = {"BINANCE_SPOT_CASH", "COINBASE_USD_CASH"}


def canonical_hash(payload):
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate(handoff, expected_side=None):
    """Validate provenance without granting timing or Entry authority."""
    handoff = dict(handoff or {})
    sealed = dict(handoff.get("sealed_payload") or {})
    side = str(sealed.get("side") or "ABSTAIN").upper()
    if not handoff:
        return False, "ACQUISITION_HANDOFF_MISSING", {}
    if not (
        handoff.get("sealed") is True
        and str(handoff.get("status") or "") == "SEALED"
        and handoff.get("authority") is False
        and handoff.get("entry_authority") is False
    ):
        return False, "ACQUISITION_HANDOFF_NOT_SEALED", {}
    if str(sealed.get("version") or "") != HANDOFF_VERSION:
        return False, "ACQUISITION_HANDOFF_VERSION_INVALID", {}
    digest = canonical_hash(sealed)
    if str(handoff.get("handoff_hash") or "") != digest:
        return False, "ACQUISITION_HANDOFF_HASH_INVALID", {}
    root_id = "cash-acquisition:%s" % digest[:20]
    if str(handoff.get("causal_wave_id") or "") != root_id:
        return False, "ACQUISITION_HANDOFF_WAVE_ID_INVALID", {}
    if side not in {"LONG", "SHORT"} or (
        expected_side is not None
        and side != str(expected_side or "ABSTAIN").upper()
    ):
        return False, "ACQUISITION_HANDOFF_SIDE_INVALID", {}
    if set(sealed.get("directional_cash_roots") or ()) != REQUIRED_ROOTS:
        return False, "ACQUISITION_HANDOFF_CASH_ROOTS_INVALID", {}
    evidence = [dict(row or {}) for row in sealed.get("segment_evidence") or ()]
    if int(sealed.get("temporal_persistence_segments", 0) or 0) < 2 or len(
        evidence
    ) < 2:
        return False, "ACQUISITION_HANDOFF_PERSISTENCE_MISSING", {}
    if not all(
        str(row.get("state") or "").upper() == "CONVERTING"
        and str(row.get("side") or "ABSTAIN").upper() == side
        and str((row.get("price") or {}).get("vote") or "").upper() == side
        and str((row.get("flow") or {}).get("vote") or "").upper() == side
        for row in evidence[:2]
    ):
        return False, "ACQUISITION_HANDOFF_SEGMENT_INVALID", {}
    onset_ms = int(sealed.get("first_converting_segment_onset_ms", 0) or 0)
    completed_ms = int(sealed.get("ownership_completed_ms", 0) or 0)
    if onset_ms <= 0 or completed_ms <= onset_ms:
        return False, "ACQUISITION_HANDOFF_TIME_INVALID", {}
    epochs = dict(sealed.get("venue_epochs") or {})
    if not all(name in epochs for name in ("spot", "coinbase")):
        return False, "ACQUISITION_HANDOFF_EPOCHS_MISSING", {}
    for name in (
        "side", "first_converting_segment_onset_ms",
        "ownership_completed_ms", "bias_version",
    ):
        if handoff.get(name) != sealed.get(name):
            return False, "ACQUISITION_HANDOFF_OUTER_FIELD_CHANGED", {}
    return True, "PASS", {
        **sealed,
        "handoff_hash": digest,
        "causal_wave_id": root_id,
        "sealed_payload": sealed,
        "status": "SEALED",
        "sealed": True,
        "authority": False,
        "entry_authority": False,
    }


def position_thesis_seed(handoff, expected_side=None):
    """Return the immutable position-owned root for a valid acquisition."""
    valid, reason, sealed = validate(handoff, expected_side)
    if not valid:
        return {
            "version": SEED_VERSION,
            "status": "UNBOUND",
            "reason": reason,
            "identity_kind": IDENTITY_KIND,
            "root_id": None,
            "root_hash": None,
            "side": str(expected_side or "ABSTAIN").upper(),
            "authority": False,
        }
    payload = dict(sealed.get("sealed_payload") or {})
    return {
        "version": SEED_VERSION,
        "status": "BOUND",
        "reason": "SEALED_BIAS_CASH_ACQUISITION_FROZEN",
        "identity_kind": IDENTITY_KIND,
        "root_id": sealed["causal_wave_id"],
        "root_hash": sealed["handoff_hash"],
        "side": sealed["side"],
        "ownership_completed_ms": sealed["ownership_completed_ms"],
        "venue_epochs": dict(sealed.get("venue_epochs") or {}),
        "cash_roots": list(sealed.get("directional_cash_roots") or ()),
        "frozen_evidence": list(payload.get("segment_evidence") or ())[:2],
        "authority": False,
    }
