"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_dong_tien
- ROLE: WebSocket: Hung goi aggTrade tu Binance Spot va Futures.
- I/O: IN: Binance Spot WS + Futures WS | OUT: RAM (bo_nho_ram)
- UPDATE: Tach ro Spot vs Futures stream cho Tri-Oracle Divergence.
- CONTRACT: DATA transport only. Optional forceOrder callbacks belong to the caller;
  this collector must not own liquidation interpretation or strategy authority.
"""

import asyncio
import orjson
import websockets
import logging
import time


async def hung_dong_tien_spot(symbol: str, bo_nho_ram):
    """
    [TIER A] AggTrade Binance SPOT - tien mat that, khong don bay.
    Ghi vao: bo_nho_ram.danh_sach_khop_lenh
    """
    url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@aggTrade"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                logging.info(f"[SPOT FLOW] Ket noi aggTrade Spot: {symbol.upper()}")
                async for raw in ws:
                    try:
                        data = orjson.loads(raw)
                        lenh_khop = {
                            'gia': float(data['p']),
                            'khoi_luong': float(data['q']),
                            'ban_chu_dong': bool(data['m']),
                            'thoi_gian_ms': int(data.get('E', time.time() * 1000)),
                            'nguon': 'SPOT'
                        }
                        bo_nho_ram.danh_sach_khop_lenh.append(lenh_khop)
                        if lenh_khop['ban_chu_dong']:
                            bo_nho_ram.spot_cvd_sell_total += lenh_khop['khoi_luong']
                        else:
                            bo_nho_ram.spot_cvd_buy_total += lenh_khop['khoi_luong']
                        bo_nho_ram.thoi_gian_dong_tien_cuoi = time.time()
                    except (KeyError, TypeError, ValueError):
                        continue
        except websockets.exceptions.ConnectionClosed as e:
            logging.warning(f"[SPOT FLOW] Mat ket noi: {e}. Ket noi lai...")
            await asyncio.sleep(3)
        except Exception as e:
            logging.error(f"[SPOT FLOW] Loi: {e}. Thu lai sau 3s...")
            await asyncio.sleep(3)


# Alias tuong thich nguoc — khoi_dong.py dang goi ten cu nay
hung_dong_tien_futures = hung_dong_tien_spot


def _apply_force_order_basic(bo_nho_ram, data):
    """Compatibility telemetry when no explicit liquidation consumer is supplied."""
    order = data.get('o', {}) or {}
    qty = float(order.get('z') or order.get('q') or 0.0)
    price = float(order.get('ap') or order.get('p') or 0.0)
    quote = max(0.0, qty * price)
    side = str(order.get('S', '')).upper()
    if side == 'SELL':
        bo_nho_ram.long_liquidation_quote_total += quote
    elif side == 'BUY':
        bo_nho_ram.short_liquidation_quote_total += quote
    bo_nho_ram.liquidation_events.append({
        'ts': float(data.get('E', time.time() * 1000)) / 1000.0,
        'side': side, 'quote': quote,
    })
    return 'BASIC_TELEMETRY'


async def hung_dong_tien_futures_real(
    symbol: str,
    bo_nho_ram,
    force_order_observer=None,
    force_order_epoch_reset=None,
):
    """
    Binance FUTURES executed flow plus forceOrder transport on one socket.

    Futures flow is derivative evidence, never independent cash direction.
    Canonical runtime may inject a forceOrder observer owned by a separate
    liquidation-context module. The same event is never processed by both the
    injected observer and this module's compatibility fallback.
    """
    streams = f"{symbol.lower()}@aggTrade/{symbol.lower()}@forceOrder"
    url = f"wss://fstream.binance.com/market/stream?streams={streams}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                if callable(force_order_epoch_reset):
                    force_order_epoch_reset(bo_nho_ram)
                logging.info(
                    "[FUTURES FLOW] Ket noi Futures aggTrade+forceOrder: %s",
                    symbol.upper(),
                )
                async for raw in ws:
                    try:
                        wrapper = orjson.loads(raw)
                        stream = str(wrapper.get('stream', ''))
                        data = wrapper.get('data', wrapper)
                        if stream.endswith('@forceOrder') or data.get('e') == 'forceOrder':
                            receive_ms = time.time() * 1000.0
                            if callable(force_order_observer):
                                result = force_order_observer(
                                    bo_nho_ram, data, receive_ms
                                )
                                bo_nho_ram.liquidation_context_last_result = str(result)
                                bo_nho_ram.liquidation_context_last_error = None
                            else:
                                bo_nho_ram.liquidation_context_last_result = (
                                    _apply_force_order_basic(bo_nho_ram, data)
                                )
                            continue
                        lenh_khop = {
                            'gia': float(data['p']),
                            'khoi_luong': float(data['q']),
                            'ban_chu_dong': bool(data['m']),
                            'thoi_gian_ms': int(data.get('E', time.time() * 1000)),
                            'nguon': 'FUTURES'
                        }
                        bo_nho_ram.danh_sach_khop_lenh_futures.append(lenh_khop)
                        if lenh_khop['ban_chu_dong']:
                            bo_nho_ram.futures_cvd_sell_total += lenh_khop['khoi_luong']
                        else:
                            bo_nho_ram.futures_cvd_buy_total += lenh_khop['khoi_luong']
                        bo_nho_ram.thoi_gian_dong_tien_futures_cuoi = time.time()
                    except (KeyError, TypeError, ValueError) as exc:
                        bo_nho_ram.liquidation_context_last_error = (
                            f"{type(exc).__name__}: {exc}"
                        )
                        continue
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed as e:
            logging.warning(f"[FUTURES FLOW] Mat ket noi: {e}. Ket noi lai...")
            await asyncio.sleep(3)
        except Exception as e:
            logging.error(f"[FUTURES FLOW] Loi: {e}. Thu lai sau 3s...")
            await asyncio.sleep(3)
