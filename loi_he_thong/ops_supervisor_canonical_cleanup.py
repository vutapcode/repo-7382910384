"""Canonical Tier-S ops cleanup applied by the safe supervisor wrapper."""

VERSION = "OPS_SUPERVISOR_CANONICAL_CLEANUP_V1"


def install(safe_module):
    ops = safe_module.ops
    services = getattr(ops, "SERVICES", None)
    if isinstance(services, dict):
        services.pop("gemini_shadow", None)
    return VERSION
