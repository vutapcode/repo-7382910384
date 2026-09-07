"""Deterministic, non-authority validation of recorded Phase-4 lifecycle events."""

import hashlib

import orjson

from loi_he_thong.market_event_contract import available_time_ms


VERSION = "PHASE4_LIFECYCLE_REPLAY_V2_VERSION_BOUND_IDENTITY"
LIFECYCLE_EVENTS = frozenset({
    "ECONOMIC_OPPORTUNITY_OPENED",
    "ECONOMIC_OPPORTUNITY_REPRICED",
    "ECONOMIC_OPPORTUNITY_LINKED",
    "ECONOMIC_OPPORTUNITY_CONSUMED",
    "ECONOMIC_OPPORTUNITY_INVALIDATED",
    "TIMING_ATTEMPT_OPENED",
    "TIMING_ATTEMPT_WAIT",
    "TIMING_ATTEMPT_PASSED",
    "TIMING_ATTEMPT_EXPIRED",
    "TIMING_ATTEMPT_CLOSED",
    "TIMING_ATTEMPT_REUSE_REJECTED",
    "ENTRY",
    "LIVE_ENTRY",
    "EXIT",
    "LIVE_EXIT",
})


class Phase4LifecycleReplay:
    """Validate the recorded transaction graph without recreating strategy authority."""

    def __init__(self):
        self.digest = hashlib.sha256()
        self.events = 0
        self.opened = set()
        self.captures = set()
        self.terminals = {}
        self.violations = []

    @staticmethod
    def _id(payload):
        try:
            return int(
                payload.get("economic_opportunity_id")
                or payload.get("canonical_opportunity_id")
                or 0
            )
        except (AttributeError, TypeError, ValueError):
            return 0

    @staticmethod
    def _key(record, body, opportunity_id):
        """Keep reused numeric counters isolated across recorder versions/waves."""
        return (
            str(record.get("code_version") or "UNKNOWN_CODE"),
            str(record.get("config_version") or "UNKNOWN_CONFIG"),
            int(opportunity_id),
            str(
                body.get("causal_wave_id")
                or body.get("causal_episode_id")
                or "UNKNOWN_WAVE"
            ),
        )

    @staticmethod
    def _key_payload(key):
        return {
            "code_version": key[0],
            "config_version": key[1],
            "opportunity_id": key[2],
            "causal_wave_id": key[3],
        }

    def observe(self, record):
        if str(record.get("stream") or "") != "bot_event":
            return
        payload = dict(record.get("payload") or {})
        event = str(payload.get("event") or "")
        if event not in LIFECYCLE_EVENTS:
            return
        body = dict(payload.get("payload") or payload)
        identity = {
            "available_time_ms": available_time_ms(record),
            "code_version": record.get("code_version"),
            "config_version": record.get("config_version"),
            "event": event,
            "causal_wave_id": body.get("causal_wave_id")
            or body.get("causal_episode_id"),
            "timing_attempt_id": body.get("timing_attempt_id"),
            "economic_opportunity_id": self._id(body),
            "payload": body,
        }
        self.digest.update(orjson.dumps(identity, option=orjson.OPT_SORT_KEYS))
        self.events += 1
        opportunity_id = identity["economic_opportunity_id"]
        opportunity_key = self._key(record, body, opportunity_id)
        if event == "ECONOMIC_OPPORTUNITY_OPENED" and opportunity_id:
            self.opened.add(opportunity_key)
        elif event in {"ENTRY", "LIVE_ENTRY"} and opportunity_id:
            self.captures.add(opportunity_key)
        elif event in {
            "ECONOMIC_OPPORTUNITY_CONSUMED",
            "ECONOMIC_OPPORTUNITY_INVALIDATED",
        } and opportunity_id:
            status = event.rsplit("_", 1)[-1]
            existing = self.terminals.get(opportunity_key)
            if existing is not None:
                self.violations.append({
                    **self._key_payload(opportunity_key),
                    "reason": "DUPLICATE_OR_CONFLICTING_TERMINAL",
                    "existing": existing,
                    "observed": status,
                })
            else:
                self.terminals[opportunity_key] = status

    def summary(self):
        violations = list(self.violations)
        for opportunity_key, status in sorted(self.terminals.items()):
            if status == "CONSUMED" and opportunity_key not in self.captures:
                violations.append({
                    **self._key_payload(opportunity_key),
                    "reason": "CONSUMED_WITHOUT_RECORDED_EXECUTABLE_CAPTURE",
                })
            if status == "INVALIDATED" and opportunity_key in self.captures:
                violations.append({
                    **self._key_payload(opportunity_key),
                    "reason": "INVALIDATED_OPPORTUNITY_HAS_CAPTURE",
                })
        if not self.terminals:
            status = "UNPROVEN_NO_PHASE4_TERMINALS"
        elif violations:
            status = "FAIL"
        else:
            status = "PASS"
        return {
            "version": VERSION,
            "authority": False,
            "status": status,
            "events": self.events,
            "opened": len(self.opened),
            "captures": len(self.captures),
            "terminals": len(self.terminals),
            "violations": violations,
            "deterministic_hash": self.digest.hexdigest(),
            "scope": "RECORDED_LIFECYCLE_VALIDATION_NOT_STRATEGY_AUTHORITY",
        }
