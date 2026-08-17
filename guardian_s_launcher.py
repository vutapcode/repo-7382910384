"""Runtime launcher for the minimal Tier-S position guardian.

Legacy SL/TP/native-stop plumbing stays intact. Manipulable discretionary early exits
are demoted: they may request a close, but only Tier-S causal convergence can grant it.
"""

import asyncio
import faulthandler
import logging
import signal
from pathlib import Path

EARLY_EXIT_REASONS_GATED = frozenset({
    "TRI_ORACLE_EJECT",
    "SHARK_ADVERSE_CONFIRMED",
    "AUG13_CAUSAL_REVERSAL",
})
TIER_S_EXIT_REASON = "TIER_S_CAUSAL_EXIT"
MONITOR_SECONDS = 0.05
IDLE_SECONDS = 0.10


def is_legacy_early_exit(reason):
    return str(reason or "").upper() in EARLY_EXIT_REASONS_GATED


def gate_legacy_reason(reason, tier_s_result):
    """Return (allowed, effective_reason). Pure helper, safe to unit-test."""
    if not is_legacy_early_exit(reason):
        return True, str(reason or "GUARDIAN")
    if isinstance(tier_s_result, dict) and tier_s_result.get("decision") == "EXIT":
        return True, TIER_S_EXIT_REASON
    return False, str(reason or "GUARDIAN")


async def _runtime():
    import khoi_dong as app

    guardian_s = app.load_module(
        "guardian_s_tier_runtime",
        Path(app.__file__).resolve().parent
        / "3_thuc_thi" / "ve_si_lenh" / "guardian_s_tier.py",
    )
    original_close = app.bao_ve_khan_cap.close_position

    async def gated_close(api, symbol, side, qty, state, reason="GUARDIAN"):
        if not is_legacy_early_exit(reason):
            return await original_close(api, symbol, side, qty, state, reason)

        position = getattr(state, "vi_the_hien_tai", None)
        if position is None or not bool(getattr(position, "active", False)):
            return False

        result = guardian_s.update_state(state, position)
        allowed, effective_reason = gate_legacy_reason(reason, result)
        if not allowed:
            state.guardian_s_suppressed_reason = str(reason)
            state.guardian_s_suppressed_at = result.get("ts", 0.0)
            logging.info(
                "[GUARDIAN-S] suppress legacy early-exit=%s decision=%svotes=%s",
                reason, result.get("decision"), result.get("votes"),
            )
            return False

        logging.warning(
            "[GUARDIAN-S] legacy request=%s granted by 3S -> %s",
            reason, effective_reason,
        )
        return await original_close(
            api, symbol, side, qty, state, effective_reason
        )

    # All legacy calls resolve the module-global close_position at call time.
    app.bao_ve_khan_cap.close_position = gated_close

    async def tier_s_monitor():
        while True:
            try:
                position = getattr(app.state, "vi_the_hien_tai", None)
                active = bool(position is not None and getattr(position, "active", False))
                busy = bool(
                    getattr(app.state, "dang_xu_ly_dong_lenh", False)
                    or getattr(app.state, "pending_close", None)
                )
                if not active or busy:
                    await asyncio.sleep(IDLE_SECONDS)
                    continue

                result = guardian_s.update_state(app.state, position)
                if result.get("decision") == "EXIT":
                    logging.warning(
                        "[GUARDIAN-S] causal EXIT side=%s confidence=%.3f votes=%s",
                        getattr(position, "side", ""),
                        float(result.get("confidence", 0.0) or 0.0),
                        result.get("votes"),
                    )
                    await original_close(
                        app.api,
                        "BTCUSDT",
                        position.side,
                        float(position.qty),
                        app.state,
                        TIER_S_EXIT_REASON,
                    )
                await asyncio.sleep(MONITOR_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never synthesize an early exit on sensor/logic failure.
                # Native exchange hard stop and legacy SL/TP remain authoritative.
                logging.exception(
                    "[GUARDIAN_S] monitor failure; fail-safe to native SL/TP",
                )
                await asyncio.sleep(0.25)

    await asyncio.gather(app.main(), tier_s_monitor())


def main():
    import khoi_dong as app

    try:
        runtime_lock = app.acquire_runtime_lock("bot")
    except app.DuplicateInstanceError as exc:
        logging.critical("[RUNTIME] %s", exc)
        raise SystemExit(73) from exc

    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        if app.uvloop is not None:
            app.uvloop.install()
        asyncio.run(_runtime())
    except KeyboardInterrupt:
        logging.info("Guardian-S launcher stopped.")
    finally:
        runtime_lock.close()


if __name__ == "__main__":
    main()
