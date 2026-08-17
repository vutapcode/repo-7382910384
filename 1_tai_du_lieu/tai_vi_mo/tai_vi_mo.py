"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_vi_mo
- ROLE: Poll Binance USD-M Open Interest + Funding, then refresh bias council.
- LOAD: no extra task/thread; council runs on this existing slow REST cadence.
"""

import asyncio
import importlib.util
import logging
import time
from pathlib import Path

import aiohttp


def _load_bias_council():
    path = Path(__file__).resolve().parents[2] / "2_suy_luan_mapping" / "bias_council.py"
    spec = importlib.util.spec_from_file_location("bias_council_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bias_council = _load_bias_council()


async def tai_du_lieu_vi_mo(symbol: str, bo_nho_ram, chu_ky_giay: int = 5):
    """Poll OI/funding and update direction-only bias on the same cadence."""
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

                result = bias_council.update_state(bo_nho_ram, now=now)
                logging.debug(
                    "[BIAS] %s conf=%.3f quorum=%s mode=%s",
                    result["bias"],
                    result["confidence"],
                    result["quorum"],
                    result["mode"],
                )

                await asyncio.sleep(chu_ky_giay)

            except aiohttp.ClientError as exc:
                logging.warning(
                    "[VI MO] Loi mang khi goi REST API: %s. Thu lai sau 5s...", exc
                )
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.error("[VI MO] Loi ngoai le: %s. Thu lai sau 10s...", exc)
                await asyncio.sleep(10)
