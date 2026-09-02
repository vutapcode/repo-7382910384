"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_vi_mo
- ROLE: Poll Binance USD-M Open Interest at a governor-friendly dynamic cadence; funding stays slow.
- CONTRACT: DATA-ONLY collector. It must never evaluate or write strategy/bias state.
"""

import asyncio
import logging
import time

import aiohttp

MIN_OI_POLL_SECONDS = 5.0


def _oi_poll_interval(bo_nho_ram, fallback=15.0):
    """Read a collection cadence hint without inspecting strategy evidence."""
    try:
        requested = float(
            getattr(bo_nho_ram, "oi_poll_interval_seconds", fallback) or fallback
        )
    except (TypeError, ValueError):
        requested = float(fallback)
    return max(5.0, min(15.0, requested))


def request_oi_refresh(bo_nho_ram, reason="RUNTIME_URGENCY"):
    """Wake the data-only OI poller without passing strategy evidence to it."""
    event = getattr(bo_nho_ram, "oi_refresh_event", None)
    bo_nho_ram.oi_refresh_requested_at = time.time()
    bo_nho_ram.oi_refresh_reason = str(reason)
    if event is None:
        bo_nho_ram.oi_refresh_pending = True
        return False
    event.set()
    return True


async def tai_du_lieu_vi_mo(symbol: str, bo_nho_ram, chu_ky_giay: float = 15.0):
    """Poll OI every 15s normally and 5s when runtime requests urgency."""
    symbol_upper = symbol.upper()
    url_oi = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol_upper}"
    url_funding = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol_upper}"
    timeout = aiohttp.ClientTimeout(total=10)
    funding_refresh_s = 15.0
    next_funding_at = 0.0
    refresh_event = asyncio.Event()
    bo_nho_ram.oi_refresh_event = refresh_event
    bo_nho_ram.open_interest_epoch = int(
        getattr(bo_nho_ram, "open_interest_epoch", 0) or 0
    ) + 1
    oi_chain_valid = False
    if bool(getattr(bo_nho_ram, "oi_refresh_pending", False)):
        refresh_event.set()
        bo_nho_ram.oi_refresh_pending = False

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            loop_started = time.monotonic()
            try:
                async with session.get(url_oi) as res_oi:
                    res_oi.raise_for_status()
                    data_oi = await res_oi.json()
                    oi_now = float(data_oi["openInterest"])

                now = time.time()
                available_mono_ns = time.monotonic_ns()
                exchange_time_ms = int(data_oi.get("time", 0) or 0)
                previous_oi = float(getattr(bo_nho_ram, "open_interest", 0.0) or 0.0)
                previous_ts = float(
                    getattr(bo_nho_ram, "open_interest_updated_at", 0.0) or 0.0
                )
                previous_exchange_ms = int(
                    getattr(
                        bo_nho_ram, "open_interest_exchange_time_ms", 0
                    ) or 0
                )
                ordered_exchange_pair = bool(
                    oi_chain_valid and previous_oi > 0.0 and previous_ts > 0.0
                    and exchange_time_ms > previous_exchange_ms > 0
                )
                if ordered_exchange_pair:
                    bo_nho_ram.prev_open_interest = previous_oi
                    bo_nho_ram.prev_open_interest_updated_at = previous_ts
                    bo_nho_ram.prev_open_interest_exchange_time_ms = (
                        previous_exchange_ms
                    )
                    bo_nho_ram.prev_open_interest_epoch = int(
                        bo_nho_ram.open_interest_epoch
                    )
                    bo_nho_ram.open_interest_change_pct = (
                        (oi_now - previous_oi) / previous_oi * 100.0
                    )
                    bo_nho_ram.open_interest_change_window_seconds = max(
                        0.0, now - previous_ts
                    )
                else:
                    bo_nho_ram.prev_open_interest = 0.0
                    bo_nho_ram.prev_open_interest_updated_at = 0.0
                    bo_nho_ram.prev_open_interest_exchange_time_ms = 0
                    bo_nho_ram.prev_open_interest_epoch = 0
                    bo_nho_ram.open_interest_change_pct = 0.0
                    bo_nho_ram.open_interest_change_window_seconds = 0.0
                bo_nho_ram.open_interest = oi_now
                bo_nho_ram.open_interest_updated_at = now
                bo_nho_ram.open_interest_available_time_ms = int(now * 1000.0)
                bo_nho_ram.open_interest_available_time_monotonic_ns = (
                    available_mono_ns
                )
                bo_nho_ram.open_interest_exchange_time_ms = exchange_time_ms
                bo_nho_ram.open_interest_source_health = "FRESH"
                oi_chain_valid = True
                bo_nho_ram.thoi_gian_vi_mo_cuoi = now

                if now >= next_funding_at:
                    async with session.get(url_funding) as res_funding:
                        res_funding.raise_for_status()
                        data_funding = await res_funding.json()
                        bo_nho_ram.funding_rate = float(data_funding["lastFundingRate"])
                        bo_nho_ram.funding_updated_at = time.time()
                    next_funding_at = now + funding_refresh_s

                bo_nho_ram.vi_mo_last_error = None
                bo_nho_ram.vi_mo_success_count = int(
                    getattr(bo_nho_ram, "vi_mo_success_count", 0) or 0
                ) + 1

                elapsed = time.monotonic() - loop_started
                interval = _oi_poll_interval(bo_nho_ram, chu_ky_giay)
                bo_nho_ram.oi_poll_interval_effective_seconds = interval
                wait_seconds = max(0.05, interval - elapsed)
                try:
                    await asyncio.wait_for(
                        refresh_event.wait(), timeout=wait_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                else:
                    refresh_event.clear()
                    # An urgent phase may interrupt a 15s normal wait, but it
                    # never turns OI REST polling into a hot loop.
                    since_poll = time.monotonic() - loop_started
                    if since_poll < MIN_OI_POLL_SECONDS:
                        await asyncio.sleep(MIN_OI_POLL_SECONDS - since_poll)

            except aiohttp.ClientError as exc:
                if oi_chain_valid:
                    bo_nho_ram.open_interest_epoch = int(
                        getattr(bo_nho_ram, "open_interest_epoch", 0) or 0
                    ) + 1
                oi_chain_valid = False
                bo_nho_ram.open_interest_source_health = "DEGRADED"
                bo_nho_ram.vi_mo_last_error = f"{type(exc).__name__}: {exc}"
                logging.warning("[VI MO] REST error: %s. Retry in 2s...", exc)
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if oi_chain_valid:
                    bo_nho_ram.open_interest_epoch = int(
                        getattr(bo_nho_ram, "open_interest_epoch", 0) or 0
                    ) + 1
                oi_chain_valid = False
                bo_nho_ram.open_interest_source_health = "DEGRADED"
                bo_nho_ram.vi_mo_last_error = f"{type(exc).__name__}: {exc}"
                logging.error("[VI MO] Unexpected error: %s. Retry in 3s...", exc)
                await asyncio.sleep(3.0)
