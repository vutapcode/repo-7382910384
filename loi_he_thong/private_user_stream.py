"""Event-driven Binance USD-M private order/account state.

REST reconciliation remains authoritative recovery. This stream shortens the
unknown-execution window and is mandatory before WStrade may arm new live entry.
"""

import asyncio
from collections import deque
import json
import logging
import time

import websockets


VERSION = "WSTRADE_PRIVATE_USER_STREAM_V2_COMMISSION_AUDIT"
STREAM_URL = "wss://fstream.binance.com/ws/{listen_key}"
KEEPALIVE_SECONDS = 25 * 60
MAX_ORDER_EVENTS = 512


def _ensure_state(state):
    if not isinstance(getattr(state, "wstrade_user_orders", None), dict):
        state.wstrade_user_orders = {}
    if not isinstance(getattr(state, "wstrade_user_event_log", None), deque):
        state.wstrade_user_event_log = deque(maxlen=MAX_ORDER_EVENTS)
    if not isinstance(getattr(state, "wstrade_user_order_commissions", None), dict):
        state.wstrade_user_order_commissions = {}
    if not isinstance(getattr(state, "wstrade_user_order_commission_trades", None), dict):
        state.wstrade_user_order_commission_trades = {}
    state.wstrade_user_stream_version = VERSION


def mark_connected(state, now=None):
    _ensure_state(state)
    now = time.time() if now is None else float(now)
    state.wstrade_user_stream_connected = True
    state.wstrade_user_stream_ready = True
    state.wstrade_user_stream_connected_at = now
    state.wstrade_user_stream_epoch = int(
        getattr(state, "wstrade_user_stream_epoch", 0) or 0
    ) + 1
    if (
        bool(getattr(state, "wstrade_live_armed", False))
        and not bool(getattr(state, "wstrade_execution_recovery_required", False))
        and not bool(getattr(state, "wstrade_live_demote_pending", False))
        and not bool(getattr(state, "execution_unknown", False))
        and (
            getattr(state, "wstrade_execution_control_plane", None) is None
            or (getattr(state, "wstrade_execution_control_plane", {}) or {}).get(
                "entry_allowed"
            ) is not False
        )
    ):
        state.wstrade_live_entry_allowed = True


def mark_disconnected(state, reason="DISCONNECTED", now=None):
    _ensure_state(state)
    now = time.time() if now is None else float(now)
    state.wstrade_user_stream_connected = False
    state.wstrade_user_stream_ready = False
    state.wstrade_user_stream_disconnected_at = now
    state.wstrade_user_stream_reason = str(reason)
    if bool(getattr(state, "wstrade_live_armed", False)):
        # Preserve exit/reconciliation authority but seal every new entry.
        state.wstrade_live_entry_allowed = False
        state.wstrade_live_arm_reason = "PRIVATE_USER_STREAM_DISCONNECTED"


def apply_event(state, payload, received_at=None):
    _ensure_state(state)
    payload = dict(payload or {})
    now = time.time() if received_at is None else float(received_at)
    event_type = str(payload.get("e", ""))
    event_time = float(payload.get("E", 0.0) or 0.0) / 1000.0 or now
    state.wstrade_user_stream_last_event_at = now
    state.wstrade_user_stream_last_exchange_event_at = event_time
    state.wstrade_user_stream_last_event_type = event_type

    if event_type == "listenKeyExpired":
        mark_disconnected(state, "LISTEN_KEY_EXPIRED", now=now)
        return "LISTEN_KEY_EXPIRED"

    if event_type == "ORDER_TRADE_UPDATE":
        order = dict(payload.get("o") or {})
        client_id = str(order.get("c", ""))
        trade_id = order.get("t")
        commission_asset = str(order.get("N") or "")
        try:
            commission_amount = float(order.get("n", 0.0) or 0.0)
        except (TypeError, ValueError):
            commission_amount = 0.0
        totals = dict(state.wstrade_user_order_commissions.get(client_id) or {})
        seen = set(state.wstrade_user_order_commission_trades.get(client_id) or ())
        fill_key = str(trade_id) if trade_id not in (None, "", 0, "0") else None
        unseen_fill = fill_key is None or fill_key not in seen
        if client_id and commission_asset and commission_amount and unseen_fill:
            totals[commission_asset] = (
                float(totals.get(commission_asset, 0.0) or 0.0)
                + commission_amount
            )
            state.wstrade_user_order_commissions[client_id] = totals
            if fill_key is not None:
                seen.add(fill_key)
                state.wstrade_user_order_commission_trades[client_id] = seen
        row = {
            "received_at": now,
            "event_time": event_time,
            "transaction_time": float(payload.get("T", 0.0) or 0.0) / 1000.0,
            "symbol": str(order.get("s", "")),
            "clientOrderId": client_id,
            "orderId": order.get("i"),
            "tradeId": trade_id,
            "side": str(order.get("S", "")),
            "positionSide": str(order.get("ps", "")),
            "type": str(order.get("o", "")),
            "executionType": str(order.get("x", "")),
            "status": str(order.get("X", "")),
            "origQty": str(order.get("q", "0")),
            "executedQty": str(order.get("z", "0")),
            "avgPrice": str(order.get("ap", "0")),
            "lastFilledQty": str(order.get("l", "0")),
            "lastFilledPrice": str(order.get("L", "0")),
            "realizedPnl": str(order.get("rp", "0")),
            "commissionAmount": str(order.get("n", "0")),
            "commissionAsset": commission_asset or None,
            "commissionByAssetCumulative": dict(totals),
        }
        state.wstrade_user_stream_last_transport_lag_ms = max(
            0.0, (now - event_time) * 1000.0
        )
        if client_id:
            state.wstrade_user_orders[client_id] = row
            while len(state.wstrade_user_orders) > MAX_ORDER_EVENTS:
                oldest = next(iter(state.wstrade_user_orders))
                state.wstrade_user_orders.pop(oldest)
                state.wstrade_user_order_commissions.pop(oldest, None)
                state.wstrade_user_order_commission_trades.pop(oldest, None)
        state.wstrade_user_event_log.append(row)
        state.wstrade_user_order_revision = int(
            getattr(state, "wstrade_user_order_revision", 0) or 0
        ) + 1
        return "ORDER_TRADE_UPDATE"

    if event_type == "ACCOUNT_UPDATE":
        account = dict(payload.get("a") or {})
        state.wstrade_user_account_update = account
        state.wstrade_user_account_revision = int(
            getattr(state, "wstrade_user_account_revision", 0) or 0
        ) + 1
        return "ACCOUNT_UPDATE"
    return "IGNORED"


def order_snapshot(state, client_id):
    _ensure_state(state)
    row = state.wstrade_user_orders.get(str(client_id))
    return dict(row) if isinstance(row, dict) else None


async def _keepalive(api, listen_key):
    while True:
        await asyncio.sleep(KEEPALIVE_SECONDS)
        _, status = await api.renew_listen_key(listen_key)
        if status != 200:
            raise RuntimeError(f"listen-key keepalive failed: HTTP {status}")


async def run(api, state, event_callback=None):
    _ensure_state(state)
    while True:
        listen_key = None
        keepalive = None
        retry_delay = 3.0
        try:
            response, status = await api.new_listen_key()
            listen_key = str((response or {}).get("listenKey", ""))
            if status != 200 or not listen_key:
                error_code = (response or {}).get("code", "UNKNOWN")
                if status in (401, 403):
                    # Authentication/configuration faults require an operator
                    # change and must not burn the host CPU in a tight loop.
                    retry_delay = 60.0
                raise RuntimeError(
                    f"listen-key creation failed: HTTP {status} code={error_code}"
                )
            async with websockets.connect(
                STREAM_URL.format(listen_key=listen_key),
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ) as websocket:
                mark_connected(state)
                if event_callback:
                    event_callback("PRIVATE_USER_STREAM_CONNECTED", {
                        "version": VERSION,
                        "epoch": state.wstrade_user_stream_epoch,
                    })
                keepalive = asyncio.create_task(_keepalive(api, listen_key))
                while True:
                    receive = asyncio.create_task(websocket.recv())
                    done, _ = await asyncio.wait(
                        (receive, keepalive),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if keepalive in done:
                        receive.cancel()
                        await asyncio.gather(receive, return_exceptions=True)
                        keepalive.result()
                    raw = receive.result()
                    result = apply_event(state, json.loads(raw))
                    if result == "ORDER_TRADE_UPDATE" and event_callback:
                        row = state.wstrade_user_event_log[-1]
                        event_callback("LIVE_ORDER_UPDATE", dict(row))
                    if result == "LISTEN_KEY_EXPIRED":
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mark_disconnected(state, f"{type(exc).__name__}: {exc}")
            logging.exception("[WSTRADE] private user stream failure")
        finally:
            if keepalive is not None:
                keepalive.cancel()
                await asyncio.gather(keepalive, return_exceptions=True)
            if listen_key:
                try:
                    await api.close_listen_key(listen_key)
                except Exception:
                    pass
            mark_disconnected(state, getattr(
                state, "wstrade_user_stream_reason", "CONNECTION_CLOSED"
            ))
        await asyncio.sleep(retry_delay)
