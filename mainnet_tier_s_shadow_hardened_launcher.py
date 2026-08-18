"""Canonical hardened Mainnet Tier-S shadow launcher."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("SMC_RUNTIME_DIR", "/home/ubuntu/.local/state/smc2026/runtime")
os.environ.setdefault("SMC_JOURNAL_DIR", "/home/ubuntu/.local/state/smc2026/mainnet_shadow")

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

close_guard = runtime.base.app.load_module(
    "close_durability_guard_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "close_durability_guard.py",
)
close_guard.install(runtime)

liveness = runtime.base.app.load_module(
    "critical_loop_liveness_runtime",
    runtime.base.app.CURRENT_DIR / "loi_he_thong" / "critical_loop_liveness.py",
)
liveness.install(runtime)

if __name__ == "__main__":
    runtime.base.main()
