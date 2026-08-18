"""Keep journal-integrity faults latched across health/readiness recomputation."""
def install(wrapper):
    health_mod = wrapper.health
    state = wrapper.base.app.state
    original = health_mod.health

    def health_with_integrity_latch(base, state_obj, now=None):
        out = original(base, state_obj, now)
        if not bool(getattr(state_obj, "shadow_integrity_fault", False)):
            return out

        reason = str(
            getattr(
                state_obj,
                "shadow_integrity_fault_reason",
                "SHADOW_INTEGRITY_FAULT",
            )
            or "SHADOW_INTEGRITY_FAULT"
        )
        out = dict(out)
        out["entry_ready"] = False
        out["full_tier_s_ready"] = False
        out["integrity_ok"] = False
        out["integrity_reason"] = reason

        state_obj.mainnet_shadow_health = out
        state_obj.mainnet_shadow_ready = False
        state_obj.system_ready = False
        state_obj.trading_enabled = False
        state_obj.last_readiness_reason = reason
        return out

    health_mod.health = health_with_integrity_latch
    return health_with_integrity_latch
