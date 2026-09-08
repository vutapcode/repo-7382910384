"""Version-bound replay adapter for the exact production authority graph.

This adapter verifies immutable decisions emitted by the production wrapper.
It deliberately does not claim to re-execute raw market events through the
live asynchronous strategy.  Until such an evaluator is attached, successful
contract replay remains non-promotional and reports that blocker explicitly.
"""

import hashlib
import json

from loi_he_thong import authority_contracts
from loi_he_thong.market_event_contract import available_time_ms


VERSION = "CANONICAL_RUNTIME_CONTRACT_REPLAY_V2_SEPARATE_CONFIG_IDENTITIES"
AUTHORITY = False


def _digest(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def inspect_loaded_runtime(lean):
    """Bind a manifest to the same Python objects used by the lean wrapper."""
    shadow = lean.shadow
    hardened_runtime = lean.hardened.runtime
    edge = hardened_runtime.edge
    state = shadow.app.state
    profile = dict(getattr(state, "strategy_profile", {}) or {})
    versions = {
        "bias": str(getattr(shadow.bias_council, "VERSION", "")),
        "ignition": str(getattr(shadow.entry_council, "INFERENCE_VERSION", "")),
        "edge": str(getattr(edge, "VERSION", "")),
        "guardian": str(getattr(shadow.guardian_s, "VERSION", "")),
    }
    checks = {
        "lean_shadow_is_hardened_base": hardened_runtime.base is shadow,
        "edge_uses_runtime_ignition": (
            getattr(edge, "ignition_core", None) is shadow.entry_council
        ),
        "entrypoint_matches_profile": (
            profile.get("canonical_entrypoint")
            == "mainnet_tier_s_lean_launcher.py"
        ),
        "active_versions_match_profile": all(
            value and value in (profile.get("architecture") or ())
            for value in (
                versions["bias"], versions["ignition"],
            )
        ),
        "version_boundary_present": bool(
            getattr(state, "code_version", "")
            and getattr(state, "strategy_config_version", "")
        ),
    }
    body = {
        "adapter_version": VERSION,
        "entrypoint": "mainnet_tier_s_lean_launcher.py",
        "code_version": str(getattr(state, "code_version", "") or ""),
        "config_version": str(
            getattr(state, "strategy_config_version", "") or ""
        ),
        "strategy_config_version": str(
            getattr(state, "strategy_config_version", "") or ""
        ),
        # Recorder configuration is a separate process identity.  It is bound
        # from the WAL/envelope, never guessed from strategy settings.
        "recorder_config_version": None,
        "profile_version": str(profile.get("version") or ""),
        "module_versions": versions,
        "object_identity_checks": checks,
        "verified": all(checks.values()),
    }
    return {**body, "manifest_hash": _digest(body)}


def bind_recording_identity(runtime_manifest, heartbeat):
    """Bind loaded code to the identity emitted by the production service."""
    body = dict(runtime_manifest or {})
    body.pop("manifest_hash", None)
    heartbeat = dict(heartbeat or {})
    observed_code = str(heartbeat.get("code_version") or "")
    observed_config = str(heartbeat.get("strategy_config_version") or "")
    checks = dict(body.get("object_identity_checks") or {})
    checks["recorded_code_matches_loaded_graph"] = bool(
        observed_code and observed_code == str(body.get("code_version") or "")
    )
    checks["recorded_config_identity_present"] = bool(observed_config)
    body["object_identity_checks"] = checks
    body["code_version"] = observed_code
    body["config_version"] = observed_config
    body["strategy_config_version"] = observed_config
    body["recording_identity_source"] = "PRODUCTION_RUNTIME_HEARTBEAT"
    body["verified"] = all(checks.values())
    return {**body, "manifest_hash": _digest(body)}


class CanonicalRuntimeContractReplay:
    """Validate recorded decisions against one exact loaded runtime manifest."""

    def __init__(self, runtime_manifest):
        self.manifest = dict(runtime_manifest or {})
        self.digest = hashlib.sha256()
        self.decision_records = 0
        self.heartbeats = 0
        self.violations = []
        self.seen_cycles = {}
        self.recorder_config_version = str(
            self.manifest.get("recorder_config_version") or ""
        )

    def _violation(self, reason, **detail):
        self.violations.append({"reason": reason, **detail})

    def observe(self, record):
        if str(record.get("stream") or "") != "bot_event":
            return
        payload = dict(record.get("payload") or {})
        body = dict(payload.get("payload") or payload)
        event = str(body.get("event") or "")
        if event == "DECISION_HEARTBEAT":
            self.heartbeats += 1
            return
        if event != "DECISION_EVALUATED":
            return

        decision = dict(body.get("decision_record") or {})
        if not decision:
            self._violation("IMMUTABLE_DECISION_RECORD_MISSING")
            return
        self.decision_records += 1
        supplied_hash = str(body.get("decision_record_hash") or "")
        actual_hash = _digest(decision)
        if not supplied_hash or supplied_hash != actual_hash:
            self._violation("DECISION_RECORD_HASH_INVALID")

        expected_code = str(self.manifest.get("code_version") or "")
        expected_config = str(
            self.manifest.get("strategy_config_version")
            or self.manifest.get("config_version") or ""
        )
        observed_code = str(decision.get("strategy_code_version") or "")
        observed_config = str(decision.get("strategy_config_version") or "")
        if observed_code != expected_code or observed_config != expected_config:
            self._violation(
                "STRATEGY_VERSION_BOUNDARY_MISMATCH",
                observed_code=observed_code,
                observed_config=observed_config,
            )
        envelope_code = str(record.get("code_version") or "")
        envelope_config = str(record.get("config_version") or "")
        if envelope_code and envelope_code != expected_code:
            self._violation("RECORDER_CODE_BOUNDARY_MISMATCH")
        if envelope_config:
            if not self.recorder_config_version:
                self.recorder_config_version = envelope_config
            elif envelope_config != self.recorder_config_version:
                self._violation(
                    "RECORDER_CONFIG_BOUNDARY_CHANGED",
                    expected_recorder_config=self.recorder_config_version,
                    observed_recorder_config=envelope_config,
                )

        bundle = body.get("authority_contracts") or decision.get(
            "authority_contracts"
        )
        if not authority_contracts.verify_bundle(bundle):
            self._violation("AUTHORITY_BUNDLE_INVALID")
        else:
            contracts = dict(bundle.get("contracts") or {})
            action = str((contracts.get("ACTION") or {}).get("action") or "")
            final_decision = str(
                (decision.get("output") or {}).get("decision") or ""
            ).upper()
            action_is_entry = action in authority_contracts.ENTRY_ACTIONS
            if (final_decision == "GO") != action_is_entry:
                self._violation(
                    "FINAL_DECISION_ACTION_CONTRADICTION",
                    final_decision=final_decision, action=action,
                )

        cycle_id = str(decision.get("cycle_id") or body.get("cycle_id") or "")
        prior = self.seen_cycles.get(cycle_id)
        if cycle_id and prior is not None and prior != actual_hash:
            self._violation("CYCLE_ID_REUSED_WITH_DIFFERENT_DECISION")
        if cycle_id:
            self.seen_cycles[cycle_id] = actual_hash
        identity = {
            "available_time_ms": available_time_ms(record),
            "cycle_id": cycle_id,
            "decision_record_hash": actual_hash,
            "authority_bundle_hash": (
                dict(bundle or {}).get("bundle_hash")
            ),
        }
        self.digest.update(
            json.dumps(
                identity, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        )

    def summary(self):
        graph_verified = bool(self.manifest.get("verified"))
        blockers = []
        if not graph_verified:
            blockers.append("PRODUCTION_OBJECT_GRAPH_NOT_VERIFIED")
        if self.decision_records == 0:
            blockers.append("NO_VERSION_BOUND_DECISION_RECORDS")
        # Contract equivalence is necessary but not sufficient to claim that
        # raw events reproduce the strategy. Keep promotion fail-closed.
        blockers.append("RAW_MARKET_DECISION_REEXECUTION_NOT_PROVEN")
        if self.violations:
            status = "FAIL"
        elif blockers[:-1]:
            status = "BLOCKED"
        else:
            status = "PASS_CONTRACT_EQUIVALENCE_ONLY"
        return {
            "version": VERSION,
            "authority": False,
            "promotion_eligible": False,
            "status": status,
            "runtime_manifest_hash": self.manifest.get("manifest_hash"),
            "strategy_config_version": str(
                self.manifest.get("strategy_config_version")
                or self.manifest.get("config_version") or ""
            ),
            "recorder_config_version": self.recorder_config_version or None,
            "decision_records": self.decision_records,
            "heartbeats": self.heartbeats,
            "violations": list(self.violations),
            "blockers": blockers,
            "deterministic_hash": self.digest.hexdigest(),
            "scope": (
                "EXACT_PRODUCTION_GRAPH_AND_RECORDED_CONTRACTS;"
                "RAW_STRATEGY_REEXECUTION_NOT_CLAIMED"
            ),
        }
