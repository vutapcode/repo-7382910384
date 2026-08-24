"""[AI_CONTEXT] NON-AUTHORITY experimental Futures top-20 depth collector.

This module is not loaded by `mainnet_tier_s_lean_launcher.py`. File existence
is not runtime authority. It must not authorize Bias, Entry, Guardian, Risk or
execution. Full recorder depth remains independent and read-only.
"""

import asyncio
import logging
import time

import orjson
import websockets


def _levels(rows, reverse=False):
    values = []
    for row in rows or ():
        try:
            price, qty = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if price > 0.0 and qty >= 0.0:
            values.append((price, qty))
    values.sort(key=lambda item: item[0], reverse=reverse)
    return values[:20]


def _changes(previous, current):
    old = {price: qty for price, qty in previous}
    new = {price: qty for price, qty in current}
    added = sum(max(0.0, qty - old.get(price, 0.0)) for price, qty in new.items())
    # A price disappearing from a partial top-20 snapshot is ambiguous: it may
    # have traded, been canceled, or merely crossed the top-20 boundary.  Only
    # a quantity decrease at a price present in both snapshots is eligible for
    # executed-flow correlation.  Keep boundary churn as telemetry, never as
    # consumption authority.
    removed = sum(
        max(0.0, qty - new[price])
        for price, qty in old.items() if price in new
    )
    boundary_removed = sum(qty for price, qty in old.items() if price not in new)
    return added, removed, boundary_removed


def apply_depth_message(state, data, now=None):
    now = time.time() if now is None else float(now)
    bids = _levels(data.get("b"), reverse=True)
    asks = _levels(data.get("a"), reverse=False)
    if not bids or not asks or asks[0][0] <= bids[0][0]:
        return False
    update_id = int(data.get("u", 0) or 0)
    previous_u = int(data.get("pu", 0) or 0)
    last_u = int(getattr(state, "futures_depth_last_u", 0) or 0)
    if last_u and previous_u and previous_u != last_u:
        state.futures_depth_gap_count = int(
            getattr(state, "futures_depth_gap_count", 0) or 0
        ) + 1
        state.futures_depth_synced = False
        return False

    old_bids = list(getattr(state, "futures_depth_bids_top_20", ()) or ())
    old_asks = list(getattr(state, "futures_depth_asks_top_20", ()) or ())
    bid_add, bid_remove, bid_boundary = _changes(old_bids, bids)
    ask_add, ask_remove, ask_boundary = _changes(old_asks, asks)
    bid_qty = sum(qty for _, qty in bids)
    ask_qty = sum(qty for _, qty in asks)
    total = bid_qty + ask_qty
    mid = (bids[0][0] + asks[0][0]) / 2.0

    state.futures_depth_bids_top_20 = bids
    state.futures_depth_asks_top_20 = asks
    state.futures_depth_last_u = update_id
    state.futures_depth_updated_at = now
    state.futures_depth_synced = True
    state.futures_depth_metrics = {
        "update_id": update_id,
        "best_bid": bids[0][0],
        "best_ask": asks[0][0],
        "mid": mid,
        "spread_bps": (asks[0][0] - bids[0][0]) / mid * 10000.0,
        "bid_qty_top20": bid_qty,
        "ask_qty_top20": ask_qty,
        "imbalance_top20": (bid_qty - ask_qty) / total if total > 0.0 else 0.0,
        "bid_replenished": bid_add,
        "ask_replenished": ask_add,
        "bid_removed": bid_remove,
        "ask_removed": ask_remove,
        "bid_removed_ambiguous_boundary": bid_boundary,
        "ask_removed_ambiguous_boundary": ask_boundary,
        "updated_at": now,
    }
    return True


async def hung_futures_depth_top20(symbol, state):
    url = f"wss://fstream.binance.com/ws/{symbol.lower()}@depth20@100ms"
    while True:
        state.futures_depth_synced = False
        state.futures_depth_epoch = int(
            getattr(state, "futures_depth_epoch", 0) or 0
        ) + 1
        state.futures_depth_last_u = 0
        try:
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=10, max_queue=64,
            ) as ws:
                logging.info(
                    "[FUTURES DEPTH] connected top20 epoch=%s",
                    state.futures_depth_epoch,
                )
                async for raw in ws:
                    try:
                        data = orjson.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not apply_depth_message(state, data):
                        if int(getattr(state, "futures_depth_last_u", 0) or 0):
                            raise ConnectionError("FUTURES_DEPTH_SEQUENCE_GAP")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.futures_depth_synced = False
            logging.warning("[FUTURES DEPTH] reconnect: %s", exc)
            await asyncio.sleep(2.0)
