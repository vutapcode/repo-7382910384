"""Canonical hardened Mainnet Tier-S shadow launcher."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("SMC_RUNTIME_DIR", "/home/ubuntu/.local/state/smc2026/runtime")
os.environ.setdefault("SMC_JOURNAL_DIR", "/home/ubuntu/.local/state/smc2026/mainnet_shadow")

subprocess.run(
    [sys.executable, str(ROOT / "ops" / "shadow_journal_recovery_guard.py")],
    check=True,
)
subprocess.run(
    [sys.executable, str(ROOT / "ops" / "shadow_journal_consistency_guard.py")],
    check=True,
)
subprocess.run(
    [sys.executable, str(ROOT / "ops" / "shadow_state_guard.py")],
    check=True,
)

import mainnet_tier_s_shadow_risk_launcher as runtime

hardening = runtime.base.app.load_module(
    "tier_s_runtime_hardening_v3",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "runtime_hardening_v3.py",
)
hardening.install(runtime)

persistence_guard = runtime.base.app.load_module(
    "persistence_decision_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "persistence_decision_guard.py",
)
persistence_guard.install(runtime)

flat_persistence_gate = runtime.base.app.load_module(
    "flat_persistence_gate_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "flat_persistence_gate.py",
)
flat_persistence_gate.install(runtime)

open_guard = runtime.base.app.load_module(
    "open_durability_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "open_durability_guard.py",
)
open_guard.install(runtime)

close_guard = runtime.base.app.load_module(
    "close_durability_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "close_durability_guard.py",
)
close_guard.install(runtime)

event_sequence_guard = runtime.base.app.load_module(
    "event_sequence_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "event_sequence_guard.py",
)
event_sequence_guard.install(runtime)

journal_health_guard = runtime.base.app.load_module(
    "journal_rollback_health_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "journal_rollback_health_guard.py",
)
journal_health_guard.install(runtime, open_guard, close_guard)

integrity_readiness_guard = runtime.base.app.load_module(
    "integrity_readiness_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "integrity_readiness_guard.py",
)
integrity_readiness_guard.install(runtime)

close_telemetry_guard = runtime.base.app.load_module(
    "close_telemetry_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "close_telemetry_guard.py",
)
close_telemetry_guard.install(runtime)

liveness = runtime.base.app.load_module(
    "critical_loop_liveness_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "critical_loop_liveness.py",
)
liveness.install(runtime)

persistence_heartbeat = runtime.base.app.load_module(
    "persistence_heartbeat_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "persistence_heartbeat.py",
)
persistence_heartbeat.install(runtime)

monotonic_heartbeat = runtime.base.app.load_module(
    "monotonic_heartbeat_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "monotonic_heartbeat.py",
)
monotonic_heartbeat.install(runtime)

futures_clock_guard = runtime.base.app.load_module(
    "futures_clock_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "futures_clock_guard.py",
)
futures_clock_guard.install(runtime)

spot_clock_guard = runtime.base.app.load_module(
    "spot_clock_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "spot_clock_guard.py",
)
spot_clock_guard.install(runtime, hardening)

data_gap_guard = runtime.base.app.load_module(
    "data_gap_taint_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "data_gap_taint_guard.py",
)
data_gap_guard.install(runtime)

supervisor_guard = runtime.base.app.load_module(
    "supervisor_return_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "supervisor_return_guard.py",
)
supervisor_guard.install(runtime.base.app)

if __name__ == "__main__":
    runtime.base.main()
