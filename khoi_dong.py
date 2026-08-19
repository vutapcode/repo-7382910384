"""Canonical Tier-S runtime kernel.

The kernel exposes one stable runtime surface for launchers:
- bootstrap/runtime primitives from tier_s_bootstrap_runtime
- active strategy/data modules from tier_s_bootstrap_runtime.m
- inert compatibility modules from the same module surface

No trading thresholds or signal logic live here.
"""

from loi_he_thong import tier_s_bootstrap_runtime as _runtime


def _export_surface(source, *, overwrite):
    for _name in dir(source):
        if _name.startswith("__"):
            continue
        if overwrite or _name not in globals():
            globals()[_name] = getattr(source, _name)


# Runtime primitives are authoritative.
_export_surface(_runtime, overwrite=True)

# Data/strategy modules are loaded once by the bootstrap module registry. Export
# the same objects here so shadow/hardened launchers and the lean task plan see
# one shared state/module graph rather than duplicate imports.
_module_surface = getattr(_runtime, "m", None)
if _module_surface is not None:
    _export_surface(_module_surface, overwrite=False)

KERNEL_VERSION = "TIER_S_RUNTIME_KERNEL_V2_MODULE_SURFACE"
