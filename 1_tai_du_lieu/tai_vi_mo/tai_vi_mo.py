"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_vi_mo
- ROLE: Poll Binance USD-M Open Interest + Funding.
- CONTRACT: DATA-ONLY collector. It must never evaluate or write strategy/bias state.
"""

import asyncio
import logging
import time

import aiohttp


async def tai_du_lieu_vi_mo(symbol: str, bo_nho_ram, chu_ky_giay: int = 5):
    """Poll OI/funding on a slow REST cadence and publish only raw macro data."""
    symbol_upper = symbol.upper()
    url_oi = (
        "https://fapi.binance.com/fapi/v1/openInterest"
        f"?symbol={symbol_upper}"
    )
    url_funding = (
        "https://fapi.binance.com/fapi/v1/premiumIndex"
        f"?symbol={symbol_upper}"
    )
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(url_oi) as res_oi:
                    res_oi.raise_for_status()
                    data_oi = await res_oi.json()
                    oi_now = float(data_oi["openInterest"])

                async with session.get(url_funding) as res_funding:
                    res_funding.raise_for_status()
                    data_funding = await res_funding.json()
                    funding_now = float(data_funding["lastFundingRate"])

                now = time.time()
                bo_nho_ram.open_interest = oi_now
                bo_nho_ram.funding_rate = funding_now
                bo_nho_ram.thoi_gian_vi_mo_cuoi = now
                bo_nho_ram.vi_mo_last_error = None
                bo_nho_ram.vi_mo_success_count = int(
                    getattr(bo_nho_ram, "vi_mo_success_count", 0) or 0
                ) + 1

                # IMPORTANT: no bias_council/update_state call here.
                # Bias has one canonical writer in the Tier-S runtime.
                await asyncio.sleep(chu_ky_giay)

            except aiohttp.ClientError as exc:
                bo_nho_ram.vi_mo_last_error = f"{type(exc).__name__}: {exc}"
                logging.warning(
                    "[VI MO] Loi mang khi goi REST API: %s. Thu lai sau 5s...",
                    exc,
                )
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                bo_nho_ram.vi_mo_last_error = f"{type(exc).__name__}: {exc}"
                logging.error("[VI MO] Loi ngoai le: %s. Thu lai sau 10s...", exc)
                await asyncio.sleep(10)
