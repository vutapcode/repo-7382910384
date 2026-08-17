"""Order-book feeds for structure and execution economics."""

import asyncio
import logging
import time

import orjson
import websockets


def _depth_rows(data):
    return (
        [[float(p), float(q)] for p, q in data.get("b", [])],
        [[float(p), float(q)] for p, q in data.get("a", [])],
    )


async def _run_depth(stream_url, symbol, state, execution=False, venue="SPOT_MAINNET"):
    while True:
        try:
            async with websockets.connect(stream_url, ping_interval=20, ping_timeout=10) as ws:
                logging.info("[ORDERBOOK%s] connected %s: %s",
                             "-EXEC" if execution else "", venue, symbol.upper())
                async for message in ws:
                    try:
                        bids, asks = _depth_rows(orjson.loads(message))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not bids or not asks:
                        continue
                    now = time.time()
                    if execution:
                        state.execution_bids = bids
                        state.execution_asks = asks
                        state.execution_bids_top_10 = bids[:10]
                        state.execution_asks_top_10 = asks[:10]
                        state.execution_depth_time = now
                    else:
                        state.bids_top_10 = bids[:10]
                        state.asks_top_10 = asks[:10]
                        state.hang_doi_so_lenh.append(
                            {"bids": bids, "asks": asks, "timestamp": now}
                        )
                        state.thoi_gian_so_lenh_cuoi = now
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed:
            logging.warning("[ORDERBOOK%s] disconnected %s; reconnecting",
                            "-EXEC" if execution else "", venue)
            await asyncio.sleep(1)
        except Exception as exc:
            logging.error("[ORDERBOOK%s] %s error: %s",
                          "-EXEC" if execution else "", venue, exc)
            await asyncio.sleep(2)


async def hung_so_lenh_futures(symbol: str, bo_nho_ram):
    """Spot Mainnet depth used for mapping/structure."""
    url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@depth20@100ms"
    await _run_depth(url, symbol, bo_nho_ram)


async def hung_so_lenh_futures_execution(symbol: str, state):
    """Futures depth from the same venue used by the execution API."""
    testnet = bool(getattr(state, "_api_is_testnet", False))
    if testnet:
        url = f"wss://stream.binancefuture.com/ws/{symbol.lower()}@depth20@100ms"
        venue = "FUTURES_TESTNET"
    else:
        url = f"wss://fstream.binance.com/ws/{symbol.lower()}@depth20@100ms"
        venue = "FUTURES_MAINNET"
    await _run_depth(url, symbol, state, execution=True, venue=venue)
