"""Fail critical if OPEN/CLOSE journal rollback cannot restore event-log consistency."""
def install(wrapper, open_guard, close_guard):
    state = wrapper.base.app.state

    def wrap(module, flag_name, reason):
        original = module._rollback_journal

        def guarded(state_obj, path, size):
            out = original(state_obj, path, size)
            if bool(getattr(state_obj, flag_name, False)):
                state_obj.shadow_integrity_fault = True
                state_obj.shadow_integrity_fault_reason = reason
                state_obj.system_ready = False
                state_obj.trading_enabled = False
                state_obj.mainnet_shadow_ready = False
                state_obj.last_readiness_reason = reason
            return out

        module._rollback_journal = guarded
        return guarded

    wrap(
        open_guard,
        "shadow_open_journal_rollback_failed",
        "SHADOW_JOURNAL_DIVERGED:open_rollback_failed",
    )
    wrap(
        close_guard,
        "shadow_journal_rollback_failed",
        "SHADOW_JOURNAL_DIVERGED:close_rollback_failed",
    )
    return True
