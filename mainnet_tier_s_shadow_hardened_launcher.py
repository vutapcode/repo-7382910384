"""Canonical hardened Mainnet Tier-S shadow launcher."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
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

if __name__ == "__main__":
    runtime.base.main()
