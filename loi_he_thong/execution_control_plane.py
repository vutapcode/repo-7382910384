"""Measured Binance execution-control-plane health.

The monitor reports transport facts; it does not infer market direction.  New
entry eligibility is relational: recently observed control latency must fit
inside the remaining opportunity budget.  No fabricated fixed latency score is
used.
"""

from collections import deque
import math
import time


VERSION = "EXECUTION_CONTROL_PLANE_V1"
MAX_SAMPLES = 256


def _percentile(values, quantile):
    values = sorted(float(value) for value in values)
    if not values:
        return None
    index = max(0, min(len(values) - 1, math.ceil(quantile * len(values)) - 1))
    return values[index]


class Monitor:
    def __init__(
        self, monotonic_clock=None, wall_clock=None,
        latency_authority_enabled=False,
    ):
        self._monotonic = monotonic_clock or time.monotonic
        self._wall = wall_clock or time.time
        self._samples = deque(maxlen=MAX_SAMPLES)
        self._inflight = {}
        self._sequence = 0
        self._latency_authority_enabled = bool(latency_authority_enabled)

    def begin(self, operation, *, control=True):
        if not control:
            return None
        self._sequence += 1
        token = self._sequence
        self._inflight[token] = {
            "operation": str(operation),
            "started_monotonic": float(self._monotonic()),
            "started_at": float(self._wall()),
        }
        return token

    def complete(self, token, status):
        if token is None:
            return None
        started = self._inflight.pop(token, None)
        if not started:
            return None
        ended_mono = float(self._monotonic())
        row = {
            **started,
            "completed_at": float(self._wall()),
            "latency_ms": max(
                0.0, (ended_mono - started["started_monotonic"]) * 1000.0
            ),
            "status": int(status),
            "success": int(status) == 200,
        }
        self._samples.append(row)
        return dict(row)

    def snapshot(self, *, opportunity_budget_ms=None, has_exposure=False):
        samples = list(self._samples)
        success = [row for row in samples if row["success"]]
        latencies = [row["latency_ms"] for row in success]
        p50 = _percentile(latencies, 0.50)
        p95 = _percentile(latencies, 0.95)
        p99 = _percentile(latencies, 0.99)
        last = samples[-1] if samples else None
        failures = sum(not row["success"] for row in samples)
        budget = (
            None if opportunity_budget_ms is None
            else max(0.0, float(opportunity_budget_ms))
        )

        reason = "NO_CONTROL_SAMPLES"
        health = "UNKNOWN"
        entry_allowed = False
        if samples:
            if last and not last["success"]:
                health = "EXIT_ONLY" if has_exposure else "UNSAFE_FOR_NEW_ENTRY"
                reason = "LATEST_CONTROL_CALL_FAILED"
            elif p95 is None:
                health = "UNKNOWN"
                reason = "NO_SUCCESSFUL_CONTROL_SAMPLE"
            elif budget is not None and p95 >= budget:
                if self._latency_authority_enabled:
                    health = (
                        "EXIT_ONLY" if has_exposure
                        else "UNSAFE_FOR_NEW_ENTRY"
                    )
                    reason = "MEASURED_P95_EXCEEDS_OPPORTUNITY_BUDGET"
                else:
                    health = "DEGRADED"
                    reason = "LATENCY_BUDGET_EXCEEDED_TELEMETRY_ONLY"
                    entry_allowed = True
            elif failures:
                health = "DEGRADED"
                reason = "RECENT_FAILURES_RECOVERED"
                entry_allowed = True
            else:
                health = "HEALTHY"
                reason = "MEASURED_CONTROL_PATH_HEALTHY"
                entry_allowed = True

        oldest = samples[0]["started_at"] if samples else None
        newest = samples[-1]["completed_at"] if samples else None
        return {
            "version": VERSION,
            "health": health,
            "reason": reason,
            "entry_allowed": bool(entry_allowed),
            "latency_authority_enabled": self._latency_authority_enabled,
            "opportunity_budget_ms": budget,
            "sample_count": len(samples),
            "successful_samples": len(success),
            "failed_samples": failures,
            "inflight_count": len(self._inflight),
            "latency_p50_ms": p50,
            "latency_p95_ms": p95,
            "latency_p99_ms": p99,
            "latest_operation": last["operation"] if last else None,
            "latest_status": last["status"] if last else None,
            "oldest_sample_at": oldest,
            "latest_sample_at": newest,
        }
