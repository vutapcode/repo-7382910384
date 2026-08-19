"""Canonical position-aware critical-loop liveness policy for Tier-S health supervision."""

VERSION = "OPS_LIVENESS_POLICY_V1_READINESS_AWARE"


def install(safe_module):
    if getattr(safe_module, "_tier_s_readiness_liveness_policy_installed", False):
        return VERSION

    ops = safe_module.ops

    def classify(heartbeat):
        try:
            installed_age = heartbeat.get("critical_liveness_installed_age_sec")
            if installed_age is None:
                return False, None
            installed_age = float(installed_age)
        except (AttributeError, TypeError, ValueError):
            return False, None

        if installed_age < 0.0:
            return True, "BIAS_LOOP_STALLED"
        if installed_age < ops.CRITICAL_LOOP_GRACE_SECONDS:
            return True, None

        loops = heartbeat.get("critical_loops") or {}
        position_active = bool(heartbeat.get("shadow_position_active", False))
        system_ready = bool(heartbeat.get("system_ready", False))

        # Bias remains the directional control loop in every state.
        limits = {"bias": ops.BIAS_ENTRY_STALE_SECONDS}

        if position_active:
            # Entry is intentionally dormant while Guardian owns an open trade.
            limits["guardian"] = ops.GUARDIAN_STALE_SECONDS
        elif system_ready:
            # Only require Entry when fresh price data makes evaluation possible.
            limits["entry"] = ops.BIAS_ENTRY_STALE_SECONDS
        # Flat + not ready is a deliberate safety wait, not an Entry stall.

        for name, limit in limits.items():
            item = loops.get(name) or {}
            age = item.get("age_sec")
            consecutive = int(item.get("consecutive_errors", 0) or 0)
            if age is None or float(age) > limit or consecutive >= 3:
                return True, f"{name.upper()}_LOOP_STALLED"
        return True, None

    safe_module._critical_loop_monotonic_classification = classify
    safe_module._tier_s_readiness_liveness_policy_installed = True
    return VERSION
