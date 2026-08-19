"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_vi_mo
- ROLE: Poll Binance USD-M Open Interest fast enough for microstructure regime; funding stays slow.
- CONTRACT: DATA-ONLY collector. It must never evaluate or write strategy/bias state.
"""

import asyncio
import logging
import time

import aiohttp


async def tai_du_lieu_vi_mo(symbol: str, bo_nho_ram, chu_ky_giay: float = 1.0):
    """Poll OI at ~1s cadence while refreshing slow funding data separately."""
    symbol_upper = symbol.upper()
    url_oi = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol_upper}"
    url_funding = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol_upper}"
    timeout = aiohttp.ClientTimeout(total=10)
    funding_refresh_s = 15.0
    next_funding_at = 0.0

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            loop_started = time.monotonic()
            try:
                async with session.get(url_oi) as res_oi:
                    res_oi.raise_for_status()
                    data_oi = await res_oi.json()
                    oi_now = float(data_oi["openInterest"])

                now = time.time()
                bo_nho_ram.open_interest = oi_now
                bo_nho_ram.open_interest_updated_at = now
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
                await asyncio.sleep(max(0.05, float(chu_ky_giay) - elapsed))

            except aiohttp.ClientError as exc:
                bo_nho_ram.vi_mo_last_error = f"{type(exc).__name__}: {exc}"
                logging.warning("[VI MO] REST error: %s. Retry in 2s...", exc)
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                bo_nho_ram.vi_mo_last_error = f"{type(exc).__name__}: {exc}"
                logging.error("[VI MO] Unexpected error: %s. Retry in 3s...", exc)
                await asyncio.sleep(3.0)
