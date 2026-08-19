"""Canonical Tier-S runtime kernel.

This module is intentionally thin: it re-exports the active bootstrap runtime
so launchers have one stable operating-system surface without duplicating
strategy or orchestration logic.
"""

from loi_he_thong import tier_s_bootstrap_runtime as _runtime

for _name in dir(_runtime):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_runtime, _name)

KERNEL_VERSION = "TIER_S_RUNTIME_KERNEL_V1"
