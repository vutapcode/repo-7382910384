"""Persistent, fail-closed Shadow -> Mainnet auto-promotion controller."""

from dataclasses import asdict, dataclass
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from datetime import datetime, timedelta, timezone

from recorder.metadata import code_version as current_code_version
from recorder.metadata import strategy_config_version as current_config_version


VERSION = "WSTRADE_AUTO_PROMOTION_V1"
VN_TZ = timezone(timedelta(hours=7))
SIGNED_TOTALS = frozenset({"realized", "stress"})


def _number(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="promotion_", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


@dataclass(frozen=True)
class PromotionState:
    version: str
    status: str
    eligible: bool
    armed: bool
    blockers: tuple
    validation_started_at: float
    validation_hours: float
    opportunity_events: int
    shadow_trades: int
    opportunity_recall: float | None
    profit_factor: float
    expectancy_usdt: float
    stress_25bps_pnl_usdt: float
    max_shadow_drawdown_usdt: float
    cpu_violation_count: int
    integrity_violation_count: int
    guardian_violation_count: int
    validation_restart_reason: str
    code_version: str
    config_version: str
    updated_at: float

    def to_dict(self):
        return asdict(self)


class PromotionController:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv(
            "WSTRADE_PROMOTION_STATE_PATH",
            "/home/ubuntu/.local/state/wstrade/promotion.json",
        ))
        self.validation_hours_required = _number("WSTRADE_VALIDATION_HOURS", 72)
        self.min_opportunities = int(_number("WSTRADE_MIN_OPPORTUNITIES", 100))
        self.min_trades = int(_number("WSTRADE_MIN_SHADOW_TRADES", 30))
        self.min_recall = _number("WSTRADE_MIN_OPPORTUNITY_RECALL", 0.70)
        self.min_profit_factor = _number("WSTRADE_MIN_PROFIT_FACTOR", 1.25)
        self.daily_loss = _number("WSTRADE_DAILY_LOSS_USDT", 0.60)
        self.replay_path = Path(os.getenv(
            "WSTRADE_REPLAY_REPORT_PATH",
            "/home/ubuntu/.local/state/wstrade/replay_validation.json",
        ))
        self.persisted = _load(self.path)

    @staticmethod
    def _versions(state):
        runtime_code = str(getattr(state, "code_version", "") or "")
        runtime_config = str(
            getattr(state, "strategy_config_version", "") or ""
        )
        root = str(getattr(state, "runtime_project_root", "") or "")
        if root:
            observed_code = current_code_version(root)
            observed_config = current_config_version()
            state.code_version_observed = observed_code
            state.strategy_config_version_observed = observed_config
        # Validation evidence belongs to the code/config imported at process
        # startup. Files changed on disk are only observations until a clean
        # restart loads them; never relabel an old-process trade as new code.
        return runtime_code, runtime_config

    @staticmethod
    def _totals(state):
        return {
            "opportunities": int(
                getattr(state, "canonical_opportunity_count", 0) or 0
            ),
            "qualified": int(
                getattr(state, "canonical_opportunity_qualified", 0) or 0
            ),
            "captured": int(
                getattr(state, "canonical_opportunity_captured", 0) or 0
            ),
            "guardian_samples": int(
                getattr(
                    state, "guardian_latency_samples_total",
                    getattr(state, "guardian_latency_samples", 0),
                ) or 0
            ),
            "trades": int(getattr(state, "mainnet_shadow_trades", 0) or 0),
            "gross_profit": float(
                getattr(state, "mainnet_shadow_gross_profit", 0.0) or 0.0
            ),
            "gross_loss": float(
                getattr(state, "mainnet_shadow_gross_loss", 0.0) or 0.0
            ),
            "realized": float(
                getattr(state, "mainnet_shadow_realized_pnl", 0.0) or 0.0
            ),
            "stress": float(
                getattr(state, "mainnet_shadow_stress_25bps_pnl", 0.0) or 0.0
            ),
        }

    @staticmethod
    def _deltas(totals, persisted):
        """Subtract the validation baseline without erasing losing PnL.

        Counters and cumulative positive ledgers are monotonic and remain
        clamped at zero for restart/migration tolerance. Realized and stress
        PnL are signed ledgers: a negative post-baseline result must reach the
        promotion gates unchanged.
        """
        delta = {}
        for name, value in totals.items():
            observed = float(value) - float(
                persisted.get(f"baseline_{name}", 0.0) or 0.0
            )
            delta[name] = observed if name in SIGNED_TOTALS else max(0.0, observed)
        return delta

    def _restart_validation(
        self, state, now, code_version, config_version, reason
    ):
        totals = self._totals(state)
        balance = float(
            getattr(state, "mainnet_shadow_balance_usdt", 0.0) or 0.0
        )
        lifetime = {
            name: int(self.persisted.get(name, 0) or 0)
            for name in (
                "lifetime_cpu_violation_count",
                "lifetime_integrity_violation_count",
                "lifetime_guardian_violation_count",
                "lifetime_drawdown_violation_count",
            )
        }
        marker = {
            "CPU_SAFETY_VIOLATION": "lifetime_cpu_violation_count",
            "INTEGRITY_VIOLATION": "lifetime_integrity_violation_count",
            "GUARDIAN_LATENCY_VIOLATION": "lifetime_guardian_violation_count",
            "SHADOW_DRAWDOWN_VIOLATION": "lifetime_drawdown_violation_count",
        }.get(reason)
        if marker:
            lifetime[marker] += 1
        self.persisted = {
            "validation_started_at": float(now),
            "code_version": code_version,
            "config_version": config_version,
            "cpu_violation_count": 0,
            "integrity_violation_count": 0,
            "guardian_violation_count": 0,
            "shadow_peak_balance": balance,
            "max_shadow_drawdown_usdt": 0.0,
            "armed": False,
            "validation_restart_reason": str(reason),
            "validation_restart_at": float(now),
            **lifetime,
            **{f"baseline_{name}": value for name, value in totals.items()},
        }

    def evaluate(self, state, now=None):
        now = time.time() if now is None else float(now)
        code_version, config_version = self._versions(state)
        observed_code = str(
            getattr(state, "code_version_observed", code_version) or ""
        )
        observed_config = str(
            getattr(
                state, "strategy_config_version_observed", config_version
            ) or ""
        )
        runtime_version_drift = bool(
            (observed_code and observed_code != code_version)
            or (observed_config and observed_config != config_version)
        )
        old_code = str(self.persisted.get("code_version", "") or "")
        old_config = str(self.persisted.get("config_version", "") or "")
        reset = bool(
            not self.persisted
            or old_code != code_version
            or old_config != config_version
        )
        if reset:
            reason = (
                "INITIAL_VALIDATION" if not self.persisted
                else "CODE_OR_CONFIG_CHANGED"
            )
            self._restart_validation(
                state, now, code_version, config_version, reason
            )
        if runtime_version_drift and str(
            self.persisted.get("validation_restart_reason", "") or ""
        ) != "RUNTIME_VERSION_DRIFT_RESTART_REQUIRED":
            self._restart_validation(
                state, now, code_version, config_version,
                "RUNTIME_VERSION_DRIFT_RESTART_REQUIRED",
            )

        raw_started = self.persisted.get("validation_started_at")
        started = float(now if raw_started is None else raw_started)
        elapsed_hours = max(0.0, (now - started) / 3600.0)
        current_balance = float(
            getattr(state, "mainnet_shadow_balance_usdt", 0.0) or 0.0
        )
        day_key = datetime.fromtimestamp(now, VN_TZ).strftime("%Y-%m-%d")
        if self.persisted.get("shadow_day_key") != day_key:
            self.persisted["shadow_day_key"] = day_key
            self.persisted["shadow_peak_balance"] = current_balance
            self.persisted["max_shadow_drawdown_usdt"] = 0.0
        peak = max(
            current_balance,
            float(self.persisted.get("shadow_peak_balance", current_balance) or current_balance),
        )
        drawdown = max(0.0, peak - current_balance)
        max_drawdown = max(
            drawdown,
            float(self.persisted.get("max_shadow_drawdown_usdt", 0.0) or 0.0),
        )
        self.persisted["shadow_peak_balance"] = peak
        self.persisted["max_shadow_drawdown_usdt"] = max_drawdown

        cpu15 = float(getattr(state, "host_cpu_15m_pct", 0.0) or 0.0)
        cpu1h = float(getattr(state, "host_cpu_1h_pct", 0.0) or 0.0)
        cpu_p95 = float(getattr(state, "host_cpu_p95_pct", 0.0) or 0.0)
        cpu_snapshot = getattr(state, "host_cpu_snapshot", {}) or {}
        cpu_coverage = bool(
            cpu_snapshot.get("coverage_15m_complete", False)
            and cpu_snapshot.get("coverage_1h_complete", False)
        )
        # Bursts are permitted by policy. Only a hard rolling-window breach
        # invalidates/restarts the soak; p95 >17 remains a promotion blocker
        # until the host is quiet enough, without erasing otherwise valid time.
        cpu_hard_windows_ok = bool(
            getattr(state, "host_cpu_hard_limit_respected", False)
            and cpu15 < 20.0 and cpu1h < 20.0
        )
        cpu_p95_ok = cpu_p95 <= 17.0
        cpu_ok = cpu_coverage and cpu_hard_windows_ok and cpu_p95_ok
        integrity_fault = bool(
            getattr(state, "shadow_integrity_fault", False)
            or getattr(state, "event_loop_stalled", False)
            or bool(getattr(state, "futures_flow_ring_saturated", False))
            or getattr(state, "execution_unknown", False)
        )
        guardian_total = int(getattr(
            state, "guardian_latency_samples_total",
            getattr(state, "guardian_latency_samples", 0),
        ) or 0)
        guardian_samples = max(
            0, guardian_total - int(
                self.persisted.get("baseline_guardian_samples", 0) or 0
            )
        )
        guardian_p95 = float(getattr(state, "guardian_latency_p95_ms", 0.0) or 0.0)
        guardian_violation = guardian_samples >= 100 and guardian_p95 > 150.0
        drawdown_violation = max_drawdown > self.daily_loss + 1e-9

        # A fault invalidates the current soak, then starts a new evidence epoch.
        # Repeated faults keep moving the start forward; a clean 72-hour period
        # can eventually recover without requiring an unrelated code edit.
        violation_reason = None
        if cpu_coverage and not cpu_hard_windows_ok:
            violation_reason = "CPU_SAFETY_VIOLATION"
        elif integrity_fault:
            violation_reason = "INTEGRITY_VIOLATION"
        elif guardian_violation:
            violation_reason = "GUARDIAN_LATENCY_VIOLATION"
        elif drawdown_violation:
            violation_reason = "SHADOW_DRAWDOWN_VIOLATION"
        if violation_reason:
            self._restart_validation(
                state, now, code_version, config_version, violation_reason
            )
            started = now
            elapsed_hours = 0.0

        totals = self._totals(state)
        delta = self._deltas(totals, self.persisted)
        opportunities = int(delta["opportunities"])
        trades = int(delta["trades"])
        qualified = int(delta["qualified"])
        captured = int(delta["captured"])
        if qualified > 0:
            recall = captured / qualified
        elif "baseline_qualified" in self.persisted:
            recall = None
        else:
            recall_raw = getattr(state, "canonical_opportunity_recall", None)
            recall = None if recall_raw is None else float(recall_raw)
        gross_profit = float(delta["gross_profit"])
        gross_loss = float(delta["gross_loss"])
        profit_factor = gross_profit / gross_loss if gross_loss > 0.0 else (
            999.0 if gross_profit > 0.0 else 0.0
        )
        realized = float(delta["realized"])
        expectancy = realized / trades if trades > 0 else 0.0
        stress_pnl = float(delta["stress"])

        blockers = []
        if runtime_version_drift:
            blockers.append("RUNTIME_VERSION_DRIFT_RESTART_REQUIRED")
        replay = _load(self.replay_path)
        replay_ok = bool(
            replay.get("passed", False)
            and replay.get("strategy_authority") == "IGNITION_CORE_V1"
            and replay.get("code_version") == code_version
            and replay.get("config_version") == config_version
        )
        if os.getenv("WSTRADE_MODE", "SHADOW").strip().upper() != "AUTO_PROMOTE":
            blockers.append("MODE_NOT_AUTO_PROMOTE")
        if os.getenv(
            "WSTRADE_IGNITION_MANUAL_APPROVAL", "false"
        ).strip().lower() not in ("1", "true", "yes", "on"):
            blockers.append("IGNITION_MANUAL_APPROVAL_REQUIRED")
        if not replay_ok:
            blockers.append("REPLAY_VALIDATION_REQUIRED")
        if elapsed_hours < self.validation_hours_required:
            blockers.append("VALIDATION_HOURS_INCOMPLETE")
        if opportunities < self.min_opportunities:
            blockers.append("OPPORTUNITIES_INSUFFICIENT")
        if trades < self.min_trades:
            blockers.append("SHADOW_TRADES_INSUFFICIENT")
        if recall is None or recall < self.min_recall:
            blockers.append("OPPORTUNITY_RECALL_BELOW_GATE")
        if profit_factor < self.min_profit_factor:
            blockers.append("PROFIT_FACTOR_BELOW_GATE")
        if expectancy <= 0.0:
            blockers.append("EXPECTANCY_NOT_POSITIVE")
        if stress_pnl < 0.0:
            blockers.append("STRESS_25BPS_NEGATIVE")
        if max_drawdown > self.daily_loss + 1e-9:
            blockers.append("SHADOW_DRAWDOWN_EXCEEDED")
        if not cpu_ok:
            blockers.append("CPU_VALIDATION_FAILED")
        if not bool(getattr(state, "lightsail_metric_fresh", False)):
            blockers.append("LIGHTSAIL_METRIC_STALE")
        external_peak = (getattr(state, "host_cpu_snapshot", {}) or {}).get(
            "max_window_pct"
        )
        if external_peak is None or float(external_peak) >= 20.0:
            blockers.append("LIGHTSAIL_CPU_NOT_BELOW_20")
        if getattr(state, "production_workload_blockers", ()):
            blockers.append("INTERACTIVE_WORKLOAD_PRESENT")
        if integrity_fault:
            blockers.append("INTEGRITY_FAULT")
        if guardian_samples < 100:
            blockers.append("GUARDIAN_LATENCY_SAMPLES_INSUFFICIENT")
        elif guardian_p95 > 150.0:
            blockers.append("GUARDIAN_LATENCY_P95_EXCEEDED")
        if getattr(state, "shadow_persistence_dirty", False):
            blockers.append("PERSISTENCE_DIRTY")
        if getattr(state, "event_loop_stalled", False):
            blockers.append("EVENT_LOOP_STALLED")

        eligible = not blockers
        armed = bool(self.persisted.get("armed", False) and not reset)
        status = "ARMED" if armed else "ELIGIBLE" if eligible else "VALIDATING"
        snapshot = PromotionState(
            version=VERSION, status=status, eligible=eligible, armed=armed,
            blockers=tuple(blockers), validation_started_at=started,
            validation_hours=elapsed_hours, opportunity_events=opportunities,
            shadow_trades=trades, opportunity_recall=recall,
            profit_factor=profit_factor, expectancy_usdt=expectancy,
            stress_25bps_pnl_usdt=stress_pnl,
            max_shadow_drawdown_usdt=max_drawdown,
            cpu_violation_count=int(self.persisted.get("cpu_violation_count", 0) or 0),
            integrity_violation_count=int(
                self.persisted.get("integrity_violation_count", 0) or 0
            ),
            guardian_violation_count=int(
                self.persisted.get("guardian_violation_count", 0) or 0
            ),
            validation_restart_reason=str(
                self.persisted.get("validation_restart_reason", "") or ""
            ),
            code_version=code_version, config_version=config_version, updated_at=now,
        ).to_dict()
        self.persisted.update(snapshot)
        _atomic_json(self.path, self.persisted)
        state.wstrade_promotion = snapshot
        state.wstrade_promotion_status = status
        return snapshot

    def mark_armed(self, state, now=None):
        self.persisted["armed"] = True
        self.persisted["armed_at"] = time.time() if now is None else float(now)
        _atomic_json(self.path, self.persisted)
        state.wstrade_promotion_status = "ARMED"

    def mark_disarmed(self, state, reason, now=None):
        self.persisted["armed"] = False
        self.persisted["disarmed_at"] = time.time() if now is None else float(now)
        self.persisted["disarm_reason"] = str(reason)
        _atomic_json(self.path, self.persisted)
        state.wstrade_promotion_status = "VALIDATING"

    async def run(
        self, state, promote_callback=None, demote_callback=None, interval=60.0
    ):
        while True:
            try:
                snapshot = await asyncio.to_thread(self.evaluate, state)
                runtime_armed = bool(getattr(state, "wstrade_live_armed", False))
                if runtime_armed and not snapshot["eligible"]:
                    if demote_callback and not bool(
                        getattr(state, "wstrade_live_demote_pending", False)
                    ):
                        await demote_callback(snapshot)
                    self.mark_disarmed(
                        state, ",".join(snapshot.get("blockers") or ("UNKNOWN",))
                    )
                elif snapshot["eligible"] and not runtime_armed and promote_callback:
                    promoted = await promote_callback(snapshot)
                    if promoted and not snapshot["armed"]:
                        self.mark_armed(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.wstrade_promotion_status = "ERROR"
                state.wstrade_promotion_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(max(5.0, float(interval)))
