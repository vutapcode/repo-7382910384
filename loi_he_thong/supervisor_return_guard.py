"""Prevent normally-returned long-lived tasks from hot-looping under the runtime supervisor."""
import asyncio
import logging


def install(app):
    state = app.state

    async def supervise(name, factory):
        while True:
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.system_ready = False
                state.trading_enabled = False
                state.last_error_source = f"supervisor:{name}"
                state.last_error_message = f"{type(exc).__name__}:{exc}"[:300]
                logging.exception("[SUPERVISOR] %s crashed; restart after 2s", name)
                await asyncio.sleep(2.0)
                continue

            state.system_ready = False
            state.trading_enabled = False
            state.last_error_source = f"supervisor:{name}"
            state.last_error_message = "TASK_RETURNED_UNEXPECTEDLY"
            logging.error("[SUPERVISOR] %s returned unexpectedly; restart after 2s", name)
            await asyncio.sleep(2.0)

    app.supervise = supervise
    return supervise
