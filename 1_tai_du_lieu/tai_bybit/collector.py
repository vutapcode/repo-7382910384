"""Optional Bybit derivative-stress recorder.

This collector is deliberately outside strategy authority.  It records public
linear-perpetual OI/mark context and complete liquidation messages so offline
same-WAL studies can distinguish venue-local Binance stress from a broader
derivative event.  It never creates LONG/SHORT direction.
"""

import asyncio
import time

import orjson
import websockets


VERSION = "BYBIT_DERIVATIVE_STRESS_RECORDER_V1"
AUTHORITY = False


def derivative_state(message):
    """Normalize one ticker message without interpreting its direction."""
    data = message.get("data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict) or data.get("openInterest") in (None, ""):
        return None
    return {
        "version": VERSION,
        "authority": AUTHORITY,
        "semantic_role": "DERIVATIVE_STRESS_DATA_ONLY",
        "symbol": data.get("symbol"),
        "open_interest": data.get("openInterest"),
        "open_interest_value": data.get("openInterestValue"),
        "last_price": data.get("lastPrice"),
        "mark_price": data.get("markPrice"),
        "index_price": data.get("indexPrice"),
        "funding_rate": data.get("fundingRate"),
        "cross_sequence": message.get("cs"),
        "source_publish_time_ms": message.get("ts"),
        "direction_authority": False,
    }


def liquidation_rows(message):
    """Preserve forced-close facts; do not convert them into trade direction."""
    result = []
    for row in message.get("data") or ():
        if not isinstance(row, dict):
            continue
        raw_side = str(row.get("S") or "").upper()
        result.append({
            "version": VERSION,
            "authority": AUTHORITY,
            "semantic_role": "FORCED_CLOSING_FLOW_ONLY",
            "symbol": row.get("s"),
            "position_side": raw_side or None,
            "liquidated_position_side": {
                "BUY": "LONG", "SELL": "SHORT",
            }.get(raw_side, "UNKNOWN"),
            "executed_size": row.get("v"),
            "bankruptcy_price": row.get("p"),
            "liquidation_time_ms": row.get("T"),
            "source_publish_time_ms": message.get("ts"),
            "direction_authority": False,
        })
    return result


async def run(recorder):
    """Record Bybit public facts with optional-source health semantics."""
    name = "bybit_research_ws"
    symbol = recorder.config.bybit_symbol
    subscribe = orjson.dumps({
        "op": "subscribe",
        "args": [f"tickers.{symbol}", f"allLiquidation.{symbol}"],
    })
    last_state_emit_ms = 0
    last_open_interest = None
    while not recorder._shutdown_requested:
        try:
            async with websockets.connect(
                recorder.config.bybit_linear_ws_url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=4 * 1024 * 1024,
                max_queue=1024,
            ) as ws:
                await ws.send(subscribe)
                recorder._advance_epoch(
                    "bybit_derivative_state", "bybit_liquidation",
                )
                recorder.health.optional_connection(name, True)
                async for raw in ws:
                    receive_ms = recorder.now_ms()
                    receive_mono_ns = time.monotonic_ns()
                    message = orjson.loads(raw)
                    topic = str(message.get("topic") or "")
                    if topic == f"tickers.{symbol}":
                        payload = derivative_state(message)
                        if payload is None:
                            continue
                        oi = payload.get("open_interest")
                        if (
                            oi == last_open_interest
                            and receive_ms - last_state_emit_ms < 5_000
                        ):
                            recorder.health.sampled_out[
                                "bybit_derivative_state"
                            ] += 1
                            continue
                        recorder.emit(
                            "bybit_derivative_state", payload,
                            event_time_ms=int(
                                payload.get("source_publish_time_ms")
                                or receive_ms
                            ),
                            sequence_start=message.get("cs"),
                            sequence_end=message.get("cs"),
                            source="bybit_linear",
                            receive_time_ms=receive_ms,
                            receive_time_monotonic_ns=receive_mono_ns,
                            feed_features=False,
                            feed_research=False,
                            source_health="RESEARCH_ONLY",
                        )
                        last_open_interest = oi
                        last_state_emit_ms = receive_ms
                    elif topic == f"allLiquidation.{symbol}":
                        for payload in liquidation_rows(message):
                            recorder.emit(
                                "bybit_liquidation", payload,
                                event_time_ms=int(
                                    payload.get("liquidation_time_ms")
                                    or payload.get("source_publish_time_ms")
                                    or receive_ms
                                ),
                                source="bybit_linear",
                                receive_time_ms=receive_ms,
                                receive_time_monotonic_ns=receive_mono_ns,
                                feed_features=False,
                                feed_research=False,
                                source_health="RESEARCH_ONLY",
                            )
                    if recorder._shutdown_requested:
                        return
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            recorder.health.optional_connection(name, False)
            recorder.health.reconnects[name] += 1
            recorder.health.optional_error(name, exc)
            await asyncio.sleep(3)
