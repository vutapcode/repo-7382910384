"""Independent USDT/USD quote-normalization collector.

Provider: Coinbase Exchange public `USDT-USD` ticker.
Role: data-only quote/basis observation.  This module never derives USDT basis
from BTCUSDT versus BTCUSD and never creates BTC direction, Bias, Entry, Exit,
or Risk authority.
"""

import asyncio
from datetime import datetime, timezone
import logging
import time

import orjson
import websockets

VERSION = "USDT_USD_BASIS_COINBASE_V1"
SOURCE_ID = "coinbase_usdt_usd"
PROVIDER = "COINBASE_EXCHANGE"
PRODUCT_ID = "USDT-USD"
SEMANTIC_ROLE = "USDT_USD_BASIS_DATA_ONLY"
AUTHORITY = False
WS_URL = "wss://ws-feed.exchange.coinbase.com"


def _event_ms(value, fallback_ms):
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1000.0)
    except (TypeError, ValueError, OverflowError):
        return int(fallback_ms)


def parse_ticker(data, receive_time_ms=None, epoch=0):
    """Normalize one Coinbase USDT-USD ticker without any BTC cross-price input."""
    payload = dict(data or {})
    if str(payload.get("type") or "") != "ticker":
        return None
    try:
        price = float(payload.get("price", 0.0) or 0.0)
        bid = float(payload.get("best_bid", 0.0) or 0.0)
        ask = float(payload.get("best_ask", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if bid > 0.0 and ask > bid and price <= 0.0:
        price = (bid + ask) / 2.0
    if price <= 0.0:
        return None
    if bid > 0.0 and ask > 0.0 and ask <= bid:
        return None

    receive_ms = int(
        time.time() * 1000.0 if receive_time_ms is None else receive_time_ms
    )
    event_ms = _event_ms(payload.get("time"), receive_ms)
    midpoint = (bid + ask) / 2.0 if bid > 0.0 and ask > bid else price
    return {
        "version": VERSION,
        "source_id": SOURCE_ID,
        "provider": PROVIDER,
        "venue": "coinbase",
        "instrument": PRODUCT_ID,
        "market_family": "QUOTE_NORMALIZATION",
        "quote_currency": "USD",
        "event_type": "BASIS_TICKER",
        "event_time_ms": event_ms,
        "receive_time_ms": receive_ms,
        "receive_time_monotonic_ns": time.monotonic_ns(),
        "epoch": int(epoch),
        "source_health": "FRESH",
        "authority": False,
        "semantic_role": SEMANTIC_ROLE,
        "usd_per_usdt": midpoint,
        "basis_bps_vs_par": (midpoint - 1.0) * 10_000.0,
        "best_bid": bid,
        "best_ask": ask,
        "sequence": payload.get("sequence"),
    }


def reset_state(state):
    state.usdt_usd_epoch = int(getattr(state, "usdt_usd_epoch", 0) or 0) + 1
    state.usdt_usd_snapshot = {
        "version": VERSION,
        "source_id": SOURCE_ID,
        "provider": PROVIDER,
        "instrument": PRODUCT_ID,
        "epoch": state.usdt_usd_epoch,
        "source_health": "WARMUP",
        "authority": False,
        "semantic_role": SEMANTIC_ROLE,
    }
    state.usdt_usd_source_health = "WARMUP"
    state.usdt_usd_updated_at = 0.0
    return state.usdt_usd_epoch


def apply_snapshot(state, snapshot):
    """Publish only namespaced normalization state; never mutate BTC fields."""
    if not isinstance(snapshot, dict):
        return False
    state.usdt_usd_snapshot = dict(snapshot)
    state.usdt_usd_source_health = str(snapshot.get("source_health") or "UNKNOWN")
    state.usdt_usd_updated_at = float(snapshot.get("receive_time_ms", 0) or 0) / 1000.0
    state.usdt_usd_basis_bps = float(snapshot.get("basis_bps_vs_par", 0.0) or 0.0)
    state.usdt_usd_price = float(snapshot.get("usd_per_usdt", 0.0) or 0.0)
    return True


async def hung_usdt_usd(state):
    """Collect public Coinbase USDT-USD ticker when an explicit caller starts it."""
    subscribe = orjson.dumps({
        "type": "subscribe",
        "product_ids": [PRODUCT_ID],
        "channels": ["ticker"],
    })
    while True:
        reset_state(state)
        try:
            async with websockets.connect(
                WS_URL, ping_interval=20, ping_timeout=20
            ) as ws:
                await ws.send(subscribe)
                logging.info(
                    "[USDT/USD] provider=%s product=%s epoch=%s authority=false",
                    PROVIDER, PRODUCT_ID, state.usdt_usd_epoch,
                )
                async for raw in ws:
                    receive_ms = int(time.time() * 1000.0)
                    try:
                        data = orjson.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    snapshot = parse_ticker(
                        data, receive_time_ms=receive_ms,
                        epoch=state.usdt_usd_epoch,
                    )
                    if snapshot is not None:
                        apply_snapshot(state, snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.usdt_usd_source_health = "DEGRADED"
            current = dict(getattr(state, "usdt_usd_snapshot", {}) or {})
            current.update(
                source_health="DEGRADED",
                last_error=f"{type(exc).__name__}: {exc}",
                authority=False,
            )
            state.usdt_usd_snapshot = current
            logging.warning("[USDT/USD] Coinbase reconnect: %s", exc)
            await asyncio.sleep(5.0)
