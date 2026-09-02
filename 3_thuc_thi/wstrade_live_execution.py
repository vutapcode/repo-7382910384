"""Minimal fail-closed WStrade Mainnet executor.

Only intents that already passed the active Tier-S councils and immutable edge
gates reach this module. One fixed 0.001 BTC position is allowed. Every fill
must acquire a verified exchange hard stop or it is immediately flattened.
"""

import asyncio
import hashlib
import math
import os
from types import SimpleNamespace
import time

from loi_he_thong import authority_contracts
from loi_he_thong import mainnet_safety
from loi_he_thong import private_user_stream
from loi_he_thong import verified_cost_model
from loi_he_thong import execution_causal_revalidation
from loi_he_thong import execution_transaction
from loi_he_thong import market_thesis


VERSION = "WSTRADE_LIVE_EXECUTION_V1"
SYMBOL = "BTCUSDT"
MAKER_TTL_SECONDS = 0.75
CAUSAL_SUBMIT_MAX_AGE_SECONDS = 1.5
BBO_SUBMIT_MAX_AGE_SECONDS = 1.0


def _execution_lock(state):
    lock = getattr(state, "_wstrade_live_execution_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        state._wstrade_live_execution_lock = lock
    return lock


def _transaction_event(state, event_callback):
    transaction = execution_transaction.snapshot(state)
    if event_callback and transaction:
        event_callback("LIVE_EXECUTION_TRANSACTION", transaction)
    return transaction


def _transition_transaction(state, target, event_callback=None, **detail):
    transaction = execution_transaction.transition(
        state, target, detail=detail,
    )
    _transaction_event(state, event_callback)
    return transaction


def _transaction_state(state):
    return str(
        (getattr(state, "wstrade_execution_transaction", {}) or {}).get(
            "state"
        ) or ""
    )


def _mark_flat_verified(state, event_callback=None, reason="RECONCILED_FLAT"):
    current = _transaction_state(state)
    if not current or current in execution_transaction.TERMINAL_STATES:
        return execution_transaction.snapshot(state)
    if current != "RECOVERY_REQUIRED":
        _transition_transaction(
            state, "RECOVERY_REQUIRED", event_callback,
            reason="RECONCILIATION_ASSUMED_AUTHORITY",
        )
    return _transition_transaction(
        state, "FLAT_VERIFIED", event_callback, reason=reason
    )


def _control_plane_snapshot(api, state, opportunity_budget_ms=None):
    getter = getattr(api, "control_plane_snapshot", None)
    if not callable(getter):
        # Test/compatibility adapters cannot claim production verification.
        return {
            "version": "EXECUTION_CONTROL_PLANE_UNAVAILABLE",
            "health": "UNKNOWN",
            "reason": "ADAPTER_HAS_NO_CONTROL_PLANE_MONITOR",
            "entry_allowed": None,
            "sample_count": 0,
        }
    position = getattr(state, "mainnet_shadow_position", None)
    snapshot = getter(
        opportunity_budget_ms=opportunity_budget_ms,
        has_exposure=bool(position and getattr(position, "active", False)),
    )
    snapshot = dict(snapshot or {})
    snapshot.update({
        "private_stream_ready": bool(
            getattr(state, "wstrade_user_stream_ready", False)
        ),
        "private_stream_transport_lag_ms": getattr(
            state, "wstrade_user_stream_last_transport_lag_ms", None
        ),
        "private_stream_last_event_at": getattr(
            state, "wstrade_user_stream_last_event_at", None
        ),
    })
    if snapshot.get("entry_allowed") is True and not snapshot[
        "private_stream_ready"
    ]:
        snapshot.update({
            "health": "EXIT_ONLY" if bool(
                getattr(state, "mainnet_shadow_position", None)
            ) else "UNSAFE_FOR_NEW_ENTRY",
            "reason": "PRIVATE_STREAM_NOT_READY",
            "entry_allowed": False,
        })
    state.wstrade_execution_control_plane = snapshot
    state.wstrade_execution_control_health = str(
        (snapshot or {}).get("health") or "UNKNOWN"
    )
    return snapshot


def _remaining_submit_budget_ms(result, now):
    decided_at = float((result or {}).get("ts", 0.0) or 0.0)
    if decided_at <= 0.0:
        return CAUSAL_SUBMIT_MAX_AGE_SECONDS * 1000.0
    return max(
        0.0,
        (decided_at + CAUSAL_SUBMIT_MAX_AGE_SECONDS - float(now)) * 1000.0,
    )


def _decision_to_submit_ms(result, now):
    decided_at = float((result or {}).get("ts", 0.0) or 0.0)
    if decided_at <= 0.0:
        return None
    return max(0.0, (float(now) - decided_at) * 1000.0)


def _revalidate_before_submit(state, side, result, now=None):
    """Compatibility wrapper around the single causal revalidation authority."""
    now = time.time() if now is None else float(now)
    ok, reason, detail = execution_causal_revalidation.validate_submit(
        state, side, result, now,
    )
    state.wstrade_live_causal_revalidation_detail = dict(detail or {})
    return ok, reason


def _replace_execution_contract(result, execution_contract):
    """Refresh only Execution's snapshot; other owner contracts stay frozen."""
    current = dict((result or {}).get("authority_contracts") or {})
    contracts = dict(current.get("contracts") or {})
    if not authority_contracts.verify(execution_contract):
        return current
    if not all(
        authority_contracts.verify(contracts.get(layer))
        for layer in ("MARKET_TRUTH", "ACTION", "SAFETY")
    ):
        return current
    updated = authority_contracts.bundle(
        contracts["MARKET_TRUTH"], contracts["ACTION"],
        execution_contract, contracts["SAFETY"],
    )
    result["authority_contracts"] = updated
    return updated


def _finalize_shadow_state(state):
    state.wstrade_live_armed = False
    state.wstrade_live_entry_allowed = False
    state.execution_allowed = False
    state.trading_enabled = False
    state.mainnet_shadow = True
    state.mainnet_shadow_real_orders_blocked = True
    state.execution_venue = "BINANCE_FUTURES_MAINNET_SHADOW"
    for name in (
        "SMC_MAINNET_ARMED", "SMC_MAINNET_EXCLUSIVE_ACCOUNT", "SMC_ENABLE_TRADING"
    ):
        os.environ[name] = "false"


def _complete_pending_demote(state):
    if not bool(getattr(state, "wstrade_live_demote_pending", False)):
        return
    _finalize_shadow_state(state)
    state.wstrade_live_demote_pending = False


TERMINAL_ORDER_STATUSES = frozenset({
    "FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED",
})


def _seal_live(state, reason, recovery=False):
    state.execution_unknown = True
    state.execution_unknown_since = time.time()
    state.execution_unknown_reason = str(reason)
    state.trading_enabled = False
    state.execution_allowed = False
    state.wstrade_live_armed = False
    state.wstrade_live_entry_allowed = False
    for name in (
        "SMC_MAINNET_ARMED", "SMC_MAINNET_EXCLUSIVE_ACCOUNT", "SMC_ENABLE_TRADING"
    ):
        os.environ[name] = "false"
    if recovery:
        state.wstrade_execution_recovery_required = True


def _executed_qty(order):
    try:
        return max(0.0, float((order or {}).get("executedQty", 0.0) or 0.0))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _commission_snapshot(state, client_id):
    row = private_user_stream.order_snapshot(state, client_id) or {}
    return {
        "client_order_id": client_id,
        "commission_amount": row.get("commissionAmount"),
        "commission_asset": row.get("commissionAsset"),
        "commission_by_asset_cumulative": dict(
            row.get("commissionByAssetCumulative") or {}
        ),
        "status": row.get("status"),
        "execution_type": row.get("executionType"),
    }


def _checkpoint_runtime(state):
    callback = getattr(state, "wstrade_runtime_state_save", None)
    if callable(callback):
        callback()


def _client_id(state, purpose, side, now):
    raw = f"{getattr(state, 'run_id', '')}|{purpose}|{side}|{int(now * 1000)}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]
    return f"ws_{purpose[:5]}_{digest}"[:36]


def _tick_price(value, tick, side):
    value, tick = float(value), max(float(tick), 1e-12)
    units = value / tick
    # A protective stop must round away from entry. Rounding toward entry can
    # silently shrink the configured 35 bps minimum below its hard bound.
    units = math.floor(units + 1e-10) if side == "LONG" else math.ceil(units - 1e-10)
    decimals = max(0, len(str(tick).rstrip("0").partition(".")[2]))
    return round(units * tick, decimals)


def _risk_geometry(state, side, price):
    price = float(price)
    qty = mainnet_safety.fixed_quantity()
    tick = float((getattr(state, "exchange_filters", {}) or {}).get("tick_size", 0.1) or 0.1)
    atr = float(getattr(state, "atr_1m", 0.0) or 0.0)
    noise_pct = (0.35 * atr / price) if price > 0.0 and atr > 0.0 else 0.0035
    stop_pct = max(0.0035, min(0.0055, noise_pct))
    stop = price * (1.0 - stop_pct if side == "LONG" else 1.0 + stop_pct)
    stop = _tick_price(stop, tick, side)
    notional = price * qty
    fee_bps = float(getattr(state, "mainnet_worst_roundtrip_fee_bps", 18.0) or 18.0)
    slippage_bps = mainnet_safety.stop_slippage_floor_bps()
    price_loss = qty * abs(price - stop)
    fee_reserve = notional * fee_bps / 10000.0
    slippage_reserve = notional * slippage_bps / 10000.0
    planned = price_loss + fee_reserve + slippage_reserve
    distance = abs(price - stop) / price
    distance_ok = 0.0035 - 1e-12 <= distance <= 0.0055 + 1e-12
    planned_ok = planned <= mainnet_safety.max_planned_loss_usdt() + 1e-9
    reason = (
        "STOP_DISTANCE_OUT_OF_BOUNDS" if not distance_ok
        else "PLANNED_LOSS_EXCEEDS_CAP" if not planned_ok
        else "PASS"
    )
    return stop, {
        "version": VERSION,
        "eligible": bool(distance_ok and planned_ok),
        "reason": reason,
        "entry_price": price,
        "bounded_hard_sl": stop,
        "stop_pct": distance,
        "price_loss_usdt": price_loss,
        "fee_reserve_usdt": fee_reserve,
        "slippage_reserve_usdt": slippage_reserve,
        "planned_worst_loss_usdt": planned,
    }


def _btc_filters(exchange_info):
    symbol = next((row for row in (exchange_info or {}).get("symbols", ()) if (
        row.get("symbol") == SYMBOL
    )), None)
    if not symbol:
        return {}
    filters = {row.get("filterType"): row for row in symbol.get("filters", ())}
    lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}
    price = filters.get("PRICE_FILTER") or {}
    notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    return {
        "step_size": float(lot.get("stepSize", 0.0) or 0.0),
        "min_qty": float(lot.get("minQty", 0.0) or 0.0),
        "max_qty": float(lot.get("maxQty", 0.0) or 0.0),
        "tick_size": float(price.get("tickSize", 0.0) or 0.0),
        "min_notional": float(
            notional.get("notional", notional.get("minNotional", 0.0)) or 0.0
        ),
    }


async def promote(api, state):
    previous = {
        name: os.environ.get(name) for name in (
            "SMC_MAINNET_ARMED", "SMC_MAINNET_EXCLUSIVE_ACCOUNT", "SMC_ENABLE_TRADING"
        )
    }
    os.environ["SMC_MAINNET_ARMED"] = "true"
    os.environ["SMC_MAINNET_EXCLUSIVE_ACCOUNT"] = "true"
    os.environ["SMC_ENABLE_TRADING"] = "true"
    try:
        if not bool(getattr(state, "wstrade_user_stream_ready", False)):
            state.wstrade_live_arm_reason = "PRIVATE_USER_STREAM_NOT_READY"
            return False
        if bool(getattr(state, "execution_unknown", False)) or bool(
            getattr(state, "wstrade_execution_recovery_required", False)
        ):
            state.wstrade_live_arm_reason = "EXECUTION_RECOVERY_REQUIRED"
            return False
        exchange_info, info_status = await api.get_exchange_info()
        filters = _btc_filters(exchange_info) if info_status == 200 else {}
        config_ok, config_reason = mainnet_safety.validate_static_config(filters)
        if not config_ok:
            state.wstrade_live_arm_reason = config_reason
            return False
        state.exchange_filters = filters
        ready, reason = await mainnet_safety.prepare_mainnet_account(api, state)
        if not ready:
            state.wstrade_live_arm_reason = reason
            return False
        balance_detail, balance_status = await api.get_balance_details()
        available = float((balance_detail or {}).get("availableBalance", 0.0) or 0.0)
        balance = float((balance_detail or {}).get("balance", available) or available)
        bid = float(getattr(state, "execution_best_bid", 0.0) or 0.0)
        ask = float(getattr(state, "execution_best_ask", 0.0) or 0.0)
        price_age = time.time() - float(getattr(state, "execution_price_time", 0.0) or 0.0)
        live_price = (bid + ask) / 2.0 if bid > 0.0 and ask > bid else 0.0
        if (
            balance_status != 200 or available <= 0.0 or live_price <= 0.0
            or price_age < 0.0 or price_age > 3.0
        ):
            state.wstrade_live_arm_reason = "MAINNET_STARTUP_MARGIN_BUFFER_INSUFFICIENT"
            return False
        _, risk_plan = _risk_geometry(state, "LONG", live_price)
        required = (
            live_price * mainnet_safety.fixed_quantity() / mainnet_safety.leverage()
            + float(risk_plan.get("planned_worst_loss_usdt", 0.0) or 0.0)
            + mainnet_safety.reserve_usdt()
        )
        state.wstrade_startup_balance_check = {
            "available_balance": available, "required_balance": required,
            "price": live_price, "risk_plan": risk_plan,
        }
        if not risk_plan.get("eligible") or available + 1e-12 < required:
            state.wstrade_live_arm_reason = "MAINNET_STARTUP_MARGIN_BUFFER_INSUFFICIENT"
            return False
        control_plane = _control_plane_snapshot(api, state)
        if control_plane.get("entry_allowed") is False:
            state.wstrade_live_arm_reason = (
                "EXECUTION_CONTROL_PLANE_UNSAFE_FOR_NEW_ENTRY"
            )
            return False
        state.balance_usdt = balance
        state.account_ready = True
        state.execution_allowed = True
        state.trading_enabled = True
        state.wstrade_live_armed = True
        state.wstrade_live_entry_allowed = True
        state.wstrade_live_demote_pending = False
        state.wstrade_live_arm_reason = "PROMOTION_GATES_PASS"
        return True
    finally:
        if not bool(getattr(state, "wstrade_live_armed", False)):
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


async def _query_fill(api, client_id, attempts=5, state=None):
    latest = None
    for _ in range(attempts):
        streamed = private_user_stream.order_snapshot(state, client_id) if state else None
        if streamed and str(streamed.get("status", "")).upper() == "FILLED":
            return streamed
        latest, status = await api.query_order(SYMBOL, client_id)
        if status == 200 and str((latest or {}).get("status", "")) == "FILLED":
            return latest
        await asyncio.sleep(0.05)
    return latest if isinstance(latest, dict) else None


async def _market_entry(api, state, side, qty, now):
    client_id = _client_id(state, "entry", side, now)
    order_side = "BUY" if side == "LONG" else "SELL"
    result, status = await api.new_order(
        SYMBOL, order_side, "MARKET", qty,
        positionSide=side, newOrderRespType="RESULT", newClientOrderId=client_id,
    )
    if status == 599:
        recovered = await _query_fill(api, client_id, state=state)
        if recovered and str(recovered.get("status", "")).upper() == "FILLED":
            return recovered, 200, client_id
        _seal_live(state, "MARKET_ENTRY_UNVERIFIED", recovery=True)
        return recovered or result, 599, client_id
    return result, status, client_id


async def _hybrid_entry(api, state, side, result, qty, now):
    if str(result.get("execution_policy", "MAKER")).upper() == "TAKER":
        return await _market_entry(api, state, side, qty, now)
    bid = float(getattr(state, "execution_best_bid", 0.0) or 0.0)
    ask = float(getattr(state, "execution_best_ask", 0.0) or 0.0)
    price = bid if side == "LONG" else ask
    client_id = _client_id(state, "maker", side, now)
    order_side = "BUY" if side == "LONG" else "SELL"
    placed, status = await api.new_order(
        SYMBOL, order_side, "LIMIT", qty, positionSide=side,
        timeInForce="GTX", price=price, newOrderRespType="ACK",
        newClientOrderId=client_id,
    )
    if status not in (200, 599):
        return placed, status, client_id
    deadline = time.monotonic() + MAKER_TTL_SECONDS
    latest = None
    while time.monotonic() < deadline:
        streamed = private_user_stream.order_snapshot(state, client_id)
        if streamed and str(streamed.get("status", "")).upper() == "FILLED":
            return streamed, 200, client_id
        latest, query_status = await api.query_order(SYMBOL, client_id)
        if query_status == 200 and str((latest or {}).get("status", "")) == "FILLED":
            return latest, 200, client_id
        await asyncio.sleep(0.05)
    order_id = (latest or placed or {}).get("orderId")
    if order_id is None:
        _seal_live(state, "MAKER_ORDER_ID_UNVERIFIED", recovery=True)
        return latest or placed, 599, client_id
    cancel_result, cancel_status = await api.cancel_order(SYMBOL, order_id)
    if cancel_status not in (200, 599):
        _seal_live(state, "MAKER_CANCEL_FAILED", recovery=True)
        return latest or cancel_result or placed, 599, client_id

    final = None
    for _ in range(5):
        candidate, query_status = await api.query_order(SYMBOL, client_id)
        if query_status == 200 and isinstance(candidate, dict):
            final = candidate
            if str(candidate.get("status", "")).upper() in TERMINAL_ORDER_STATUSES:
                break
        await asyncio.sleep(0.05)
    final_status = str((final or {}).get("status", "")).upper()
    if final_status == "FILLED":
        return final, 200, client_id
    if final_status not in TERMINAL_ORDER_STATUSES:
        _seal_live(state, "MAKER_CANCEL_UNVERIFIED", recovery=True)
        return final or cancel_result or placed, 599, client_id
    if _executed_qty(final) > 0.0:
        # Caller will flatten only after the remaining maker quantity is
        # proven terminal, preventing a late fill from reopening exposure.
        return final, 409, client_id
    # A real maker timeout is not permission to chase.  A taker conversion is
    # first evaluated in shadow against the same causal episode and Guardian.
    return final or placed, 409, client_id

async def _place_stop(api, state, position, event_callback=None):
    client_id = _client_id(state, "stop", position.side, time.time())
    params = {
        "symbol": SYMBOL,
        "side": "SELL" if position.side == "LONG" else "BUY",
        "type": "STOP_MARKET",
        "triggerPrice": str(position.hard_sl),
        "workingType": "MARK_PRICE",
        "closePosition": "true",
        "positionSide": position.side,
        "clientAlgoId": client_id,
    }
    _transition_transaction(
        state, "PROTECTION_SENT", event_callback,
        client_algo_id=client_id,
        trigger_price=str(position.hard_sl),
    )
    position.hard_sl_client_algo_id = client_id
    try:
        _checkpoint_runtime(state)
    except Exception as exc:
        state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
        state.shadow_persistence_dirty = True
        _seal_live(state, "PROTECTION_INTENT_CHECKPOINT_FAILED", recovery=True)
        return False, {
            "post_status": None,
            "verification_status": None,
            "reason": "PROTECTION_INTENT_CHECKPOINT_FAILED",
        }
    response, status = await api.new_algo_order(**params)
    if status == 200 and (response or {}).get("algoId") is not None:
        _transition_transaction(
            state, "PROTECTION_ACKNOWLEDGED", event_callback,
            client_algo_id=client_id,
            algo_id=(response or {}).get("algoId"),
            http_status=status,
        )
    # A successful POST is only an acknowledgment.  Protection becomes true
    # after an independent exchange read proves the stop is currently open.
    open_status = 599
    recovered = None
    for attempt in range(3):
        open_algos, open_status = await api.get_open_algo_orders(SYMBOL)
        recovered = next(
            (
                row for row in open_algos
                if str(row.get("clientAlgoId") or "") == client_id
                or (
                    (response or {}).get("algoId") is not None
                    and str(row.get("algoId")) == str(
                        (response or {}).get("algoId")
                    )
                )
            ),
            None,
        ) if open_status == 200 and isinstance(open_algos, list) else None
        if recovered or attempt == 2:
            break
        await asyncio.sleep(0.05)
    if recovered:
        position.hard_sl_algo_id = recovered.get("algoId")
        position.hard_sl_client_algo_id = recovered.get(
            "clientAlgoId", client_id
        )
        _transition_transaction(
            state, "PROTECTION_VERIFIED", event_callback,
            client_algo_id=position.hard_sl_client_algo_id,
            algo_id=position.hard_sl_algo_id,
            verification_source="OPEN_ALGO_ORDERS",
            verification_status=open_status,
        )
        return True, recovered
    _transition_transaction(
        state, "PROTECTION_VERIFICATION_FAILED", event_callback,
        client_algo_id=client_id,
        post_status=status,
        verification_status=open_status,
        post_response=dict(response or {}),
    )
    return False, {
        "post_response": response,
        "post_status": status,
        "verification_status": open_status,
        "verification_source": "OPEN_ALGO_ORDERS",
    }


async def _emergency_flatten(
    api, state, side, qty, *, event_callback=None, transaction_reason=None,
):
    transaction = execution_transaction.snapshot(state) or {}
    existing_id = str(
        transaction.get("emergency_flatten_client_order_id") or ""
    )
    if existing_id:
        recovered = await _query_fill(api, existing_id, state=state)
        if recovered and str(recovered.get("status", "")).upper() == "FILLED":
            result, status = recovered, 200
        else:
            _seal_live(
                state, "EMERGENCY_FLATTEN_RECONCILIATION_PENDING",
                recovery=True,
            )
            return recovered or {
                "clientOrderId": existing_id,
                "status": "UNKNOWN",
            }, 599
        client_id = existing_id
    else:
        client_id = _client_id(state, "panic", side, time.time())
        if transaction_reason:
            _transition_transaction(
                state, "EMERGENCY_FLATTEN_SENT", event_callback,
                reason=str(transaction_reason), quantity=float(qty),
                client_order_id=client_id,
            )
            try:
                _checkpoint_runtime(state)
            except Exception as exc:
                state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
                state.shadow_persistence_dirty = True
                _seal_live(
                    state, "EMERGENCY_FLATTEN_INTENT_CHECKPOINT_FAILED",
                    recovery=True,
                )
                # Persistence loss must never suppress the physical risk action.
                # Continue with the idempotent client id and keep recovery sealed.
        result, status = await api.new_order(
            SYMBOL, "SELL" if side == "LONG" else "BUY", "MARKET", qty,
            positionSide=side, newOrderRespType="RESULT",
            newClientOrderId=client_id,
        )
        if status == 599:
            recovered = await _query_fill(api, client_id, state=state)
            if recovered and str(recovered.get("status", "")) == "FILLED":
                result, status = recovered, 200
    if status != 200 or str((result or {}).get("status", "")) not in (
        "FILLED", "FILLED_RECOVERED_FROM_POSITION"
    ):
        _seal_live(state, "EMERGENCY_FLATTEN_UNVERIFIED", recovery=True)
        if transaction_reason:
            _transition_transaction(
                state, "RECOVERY_REQUIRED", event_callback,
                reason="EMERGENCY_FLATTEN_UNVERIFIED",
                client_order_id=client_id,
                http_status=status,
            )
        status = 599
    elif transaction_reason:
        # A closing fill is not enough: independently verify the account is
        # flat before clearing exposure or publishing a terminal outcome.
        positions, position_status = await api.get_positions(SYMBOL)
        still_active = position_status != 200 or any(
            abs(float(row.get("positionAmt", 0.0) or 0.0)) > 0.0
            for row in (positions or [])
        )
        if still_active:
            _seal_live(
                state, "POST_FILL_ABORT_RECONCILIATION_REQUIRED",
                recovery=True,
            )
            _transition_transaction(
                state, "RECOVERY_REQUIRED", event_callback,
                reason="FLATTEN_FILLED_ACCOUNT_RECONCILIATION_PENDING",
                client_order_id=client_id,
                http_status=status,
                position_query_status=position_status,
            )
            status = 599
        else:
            local = getattr(state, "mainnet_shadow_position", None)
            if local is not None:
                local.active = False
            state.mainnet_shadow_position = None
            state.mainnet_shadow_position_status = "FLAT"
            state.wstrade_live_position = None
            state.wstrade_unprotected_exposure = False
            state.wstrade_execution_recovery_required = False
            state.execution_unknown = False
            _mark_flat_verified(
                state, event_callback, reason="EMERGENCY_FLATTEN_VERIFIED_FLAT"
            )
            _finalize_shadow_state(state)
            try:
                _checkpoint_runtime(state)
            except Exception as exc:
                state.wstrade_live_checkpoint_error = (
                    f"{type(exc).__name__}: {exc}"
                )
                state.shadow_persistence_dirty = True
                state.wstrade_execution_recovery_required = True
                state.execution_unknown = True
                status = 599
    return result, status


def _fill_price(order, fallback=0.0):
    try:
        return float((order or {}).get("avgPrice", 0.0) or fallback or 0.0)
    except (AttributeError, TypeError, ValueError):
        return float(fallback or 0.0)


def _record_aborted_fill(
    state, result, side, executed_qty, entry_order, flatten_order,
    flatten_status, reason, event_callback=None,
):
    """Publish an explicit fill lifecycle even when no position is adopted."""
    result = dict(result or {})
    qty = max(0.0, float(executed_qty or 0.0))
    entry_price = _fill_price(entry_order)
    flatten_price = _fill_price(flatten_order)
    flatten_verified = bool(flatten_status == 200)
    recovery = bool(
        getattr(state, "execution_unknown", False)
        or getattr(state, "wstrade_execution_recovery_required", False)
        or not flatten_verified
    )
    side_sign = 1.0 if str(side).upper() == "LONG" else -1.0
    gross_bps = (
        side_sign * (flatten_price - entry_price) / entry_price * 10_000.0
        if entry_price > 0.0 and flatten_price > 0.0 else None
    )
    entry_style = (
        "TAKER"
        if str(result.get("execution_policy") or "MAKER").upper() == "TAKER"
        else "MAKER"
    )
    modeled = verified_cost_model.estimate(
        dict(result, phase="RELEASE" if entry_style == "TAKER" else "ACCEPTANCE"),
        state,
    )
    fee_bps = float(modeled.get("entry_fee_bps", 0.0) or 0.0) + float(
        modeled.get("exit_fee_bps", 0.0) or 0.0
    )
    outcome = {
        "version": "LIVE_ENTRY_OUTCOME_V1",
        "status": (
            "FILLED_THEN_FLATTENED"
            if flatten_verified and not recovery else "FILL_RECOVERY_PENDING"
        ),
        "capture_required": bool(flatten_verified and not recovery),
        "reservation_hold_required": bool(recovery),
        "canonical_opportunity_id": int(
            result.get("canonical_opportunity_id", 0) or 0
        ),
        "causal_episode_id": result.get("causal_episode_id"),
        "decision_cycle_id": result.get("decision_cycle_id"),
        "side": str(side).upper(),
        "executed_qty": qty,
        "entry_fill_price": entry_price or None,
        "flatten_fill_price": flatten_price or None,
        "flatten_verified": flatten_verified,
        "reason": str(reason),
        "gross_pnl_bps": round(gross_bps, 6) if gross_bps is not None else None,
        "fee_bps": round(fee_bps, 6),
        "net_pnl_bps": (
            round(gross_bps - fee_bps, 6) if gross_bps is not None else None
        ),
        "commission_verified": bool(modeled.get("commission_verified")),
        "final_event_emitted": False,
    }
    state.wstrade_live_last_entry_outcome = outcome
    if outcome["capture_required"]:
        _emit_final_aborted_fill(state, event_callback)
    elif event_callback:
        event_callback("ENTRY_FILL_RECOVERY_PENDING", dict(outcome))
    return outcome


def _emit_final_aborted_fill(state, event_callback, flatten_order=None):
    outcome = dict(
        getattr(state, "wstrade_live_last_entry_outcome", {}) or {}
    )
    if not outcome or float(outcome.get("executed_qty", 0.0) or 0.0) <= 0.0:
        return False
    if flatten_order is not None:
        price = _fill_price(flatten_order)
        if price > 0.0:
            outcome["flatten_fill_price"] = price
    entry_price = float(outcome.get("entry_fill_price", 0.0) or 0.0)
    flatten_price = float(outcome.get("flatten_fill_price", 0.0) or 0.0)
    if entry_price > 0.0 and flatten_price > 0.0:
        direction = 1.0 if str(outcome.get("side")).upper() == "LONG" else -1.0
        gross_bps = direction * (flatten_price - entry_price) / entry_price * 10_000.0
        outcome["gross_pnl_bps"] = round(gross_bps, 6)
        outcome["net_pnl_bps"] = round(
            gross_bps - float(outcome.get("fee_bps", 0.0) or 0.0), 6
        )
    outcome.update({
        "status": "FILLED_THEN_FLATTENED",
        "capture_required": True,
        "reservation_hold_required": False,
        "flatten_verified": True,
    })
    already_emitted = bool(outcome.get("final_event_emitted"))
    outcome["final_event_emitted"] = True
    state.wstrade_live_last_entry_outcome = outcome
    if event_callback and not already_emitted:
        event_callback("ENTRY_FILLED_THEN_FLATTENED", {
            **outcome,
            "miss_taxonomy": "LIVE_FILLED_THEN_FLATTENED",
            "failed_gates": [str(outcome.get("reason") or "ABORTED_AFTER_FILL")],
            "counterfactual": {
                "eligible": bool(entry_price > 0.0),
                "reference_price": entry_price or None,
                "side": outcome.get("side"),
                "hard_sl_bps": None,
                "windows_seconds": [5, 15, 30, 60],
            },
        })
    return True


def _entry_causal_thesis(result):
    """Keep only the cash-led facts Guardian needs to judge thesis failure."""
    causal=(result or {}).get("causal") or {}
    ignition=(result or {}).get("ignition") or causal.get("ignition") or {}
    edge=dict((result or {}).get("edge_tier") or {})
    groups=causal.get("evidence_groups") or {}
    cash_price=set(groups.get("cash_price") or ())&{"spot","coinbase"}
    cash_flow=set(groups.get("cash_flow") or ())&{"spot","coinbase"}
    anchors=cash_price|cash_flow
    joint=cash_price&cash_flow
    handoff=str((causal.get("handoff") or {}).get("status") or "").upper()
    if handoff=="SPOT_HANDOFF" and "spot" in anchors:
        primary="spot"
    elif "spot" in joint:
        primary="spot"
    elif "coinbase" in joint:
        primary="coinbase"
    elif "spot" in anchors:
        primary="spot"
    elif "coinbase" in anchors:
        primary="coinbase"
    else:
        primary=None
    if ignition:
        frozen=dict(ignition.get("bias_snapshot") or {})
        context=dict(frozen.get("direction_context") or {})
        cash_names=set(ignition.get("cash_venues") or ())
        cash_aliases={"spot" if x=="binance_spot" else "coinbase" for x in cash_names}
        if ignition.get("proposer")=="binance_spot": primary="spot"
        elif ignition.get("proposer")=="coinbase_spot": primary="coinbase"
        elif "spot" in cash_aliases: primary="spot"
        elif "coinbase" in cash_aliases: primary="coinbase"
        thesis_contract = market_thesis.build(
            result, primary_cash_anchor=primary, cash_anchors=cash_aliases,
        )
        return {
            "version":"ENTRY_CAUSAL_THESIS_V2_MARKET_THESIS_CONTRACT",
            "market_thesis":thesis_contract,
            "authority_basis":(result or {}).get("authority_basis"),
            "authority_dependencies":dict(
                (result or {}).get("authority_dependencies") or {}
            ),
            "authority_proof_hash":(result or {}).get("authority_proof_hash"),
            "primary_cash_anchor":primary,
            "cash_anchors":sorted(cash_aliases),
            "handoff_status":str(ignition.get("leader") or "UNKNOWN"),
            "oi_intent": dict(ignition.get("oi_intent") or {}),
            "proof_type":ignition.get("proof_type"),
            "proposer":ignition.get("proposer"),
            "impulse_phase":ignition.get("impulse_phase"),
            "residual_edge_proxy_bps":ignition.get("residual_edge_proxy_bps"),
            "economic_contract_version":edge.get("economic_contract_version"),
            "economic_feature_snapshot":dict(
                edge.get("economic_feature_snapshot") or {}
            ),
            "forward_edge":dict(edge.get("forward_edge") or {}),
            "time_to_edge":dict(edge.get("time_to_edge") or {}),
            "execution_urgency":dict(edge.get("execution_urgency") or {}),
            "flow_efficiency":dict(ignition.get("flow_efficiency") or {}),
            "oi_verification_state":dict(
                ignition.get("oi_verification_state") or {}
            ),
            "bias_thesis":{
                "direction":frozen.get("direction"),
                "confidence":frozen.get("confidence"),
                "context_side":context.get("context_side"),
                "phase":context.get("phase"),
                "candidate_side":context.get("candidate_side"),
                "hysteresis":context.get("hysteresis"),
                "price_vote":context.get("price_vote"),
                "flow_vote":context.get("flow_vote"),
                "oi_regime":context.get("oi_regime"),
            },
        }
    return {
        "version":"ENTRY_CAUSAL_THESIS_V1_LEGACY_READ_ONLY",
        "primary_cash_anchor":primary,
        "cash_anchors":sorted(anchors),
        "handoff_status":handoff or None,
        "oi_intent":dict(causal.get("oi_intent") or {}),
    }

def _position(side, qty, fill_price, hard_sl, risk_plan, now, client_id, result):
    r_value = abs(fill_price - hard_sl)
    fee_r = (
        fill_price * float(risk_plan.get("fee_reserve_usdt", 0.0) or 0.0)
        / max(fill_price * qty * r_value, 1e-12)
    )
    return SimpleNamespace(
        active=True, live=True, side=side, qty=qty, initial_qty=qty,
        opened_at=now, position_cycle_id=f"live:{side}:{int(now * 1000)}",
        entry_price=fill_price, execution_entry_price=fill_price,
        hard_sl=hard_sl, r=r_value, best=fill_price, best_r=0.0,
        floor_r=None, floor=None, stage="INITIAL", tier_mode="PROTECT",
        # Deprecated checkpoint compatibility only. The active risk module
        # never reads these fields and cannot emit a Whale Exhaustion exit.
        fee_r=fee_r, whale_seen=False, whale_exhaustion_since=0.0,
        whale_exhaustion_pressure=0.0, risk_px_samples=[], exhaustion_meta={},
        entry_client_order_id=client_id, hard_sl_algo_id=None,
        hard_sl_client_algo_id=None, mainnet_risk_plan=dict(risk_plan),
        entry_lane=result.get("lane", "CORE"),
        decision_cycle_id=result.get("decision_cycle_id"),
        canonical_opportunity_id=int(result.get("canonical_opportunity_id", 0) or 0),
        causal_episode_id=result.get("causal_episode_id"),
        authority_contracts=dict(result.get("authority_contracts") or {}),
        entry_causal_thesis=_entry_causal_thesis(result),
        edge_first_positive_net_at=None,
        edge_time_to_positive_net_seconds=None,
    )


async def _open_position_locked(
    api, state, side, result, now=None, event_callback=None
):
    now = time.time() if now is None else float(now)
    result = dict(result or {})
    state.wstrade_live_last_entry_outcome = {
        "version": "LIVE_ENTRY_OUTCOME_V1",
        "status": "NOT_SUBMITTED",
        "capture_required": False,
        "reservation_hold_required": False,
        "canonical_opportunity_id": int(
            result.get("canonical_opportunity_id", 0) or 0
        ),
        "causal_episode_id": result.get("causal_episode_id"),
    }
    if not bool(getattr(state, "wstrade_live_armed", False)):
        return None
    if not bool(getattr(state, "wstrade_live_entry_allowed", True)):
        return None
    if bool(getattr(state, "execution_unknown", False)) or bool(
        getattr(state, "wstrade_execution_recovery_required", False)
    ):
        return None
    today = mainnet_safety.vn_day_start_ms(now)
    if bool(getattr(state, "wstrade_daily_locked", False)):
        if int(getattr(state, "wstrade_daily_lock_day", 0) or 0) == today:
            return None
        state.wstrade_daily_locked = False
    if getattr(state, "mainnet_shadow_position", None) is not None and bool(
        getattr(state.mainnet_shadow_position, "active", False)
    ):
        return None
    bid = float(getattr(state, "execution_best_bid", 0.0) or 0.0)
    ask = float(getattr(state, "execution_best_ask", 0.0) or 0.0)
    price = ask if side == "LONG" else bid
    if price <= 0.0:
        return None
    hard_sl, risk_plan = _risk_geometry(state, side, price)
    gate_ok, gate_reason, gate_detail = await mainnet_safety.exchange_entry_gate(
        api, state, price, hard_sl, risk_plan=risk_plan
    )
    state.wstrade_live_last_entry_gate = {
        "ok": gate_ok, "reason": gate_reason, "detail": gate_detail,
    }
    if not gate_ok:
        return None
    control_plane = _control_plane_snapshot(
        api, state,
        opportunity_budget_ms=_remaining_submit_budget_ms(result, time.time()),
    )
    state.wstrade_live_last_control_plane_gate = dict(control_plane)
    if control_plane.get("entry_allowed") is False:
        state.wstrade_live_last_entry_gate = {
            "ok": False,
            "reason": "EXECUTION_CONTROL_PLANE_UNSAFE_FOR_NEW_ENTRY",
            "detail": dict(control_plane),
        }
        if event_callback:
            event_callback("LIVE_ENTRY_CONTROL_PLANE_REJECTED", {
                "causal_episode_id": result.get("causal_episode_id"),
                "canonical_opportunity_id": result.get(
                    "canonical_opportunity_id"
                ),
                "side": side,
                "control_plane": dict(control_plane),
            })
        return None
    if (
        bool(getattr(state, "wstrade_live_armed", False))
        and int((result or {}).get("canonical_opportunity_id", 0) or 0) > 0
    ):
        causal_ok, causal_reason = _revalidate_before_submit(
            state, side, result, now=time.time(),
        )
        state.wstrade_live_last_causal_revalidation = {
            "ok": causal_ok,
            "reason": causal_reason,
            "detail": dict(
                getattr(
                    state, "wstrade_live_causal_revalidation_detail", {}
                ) or {}
            ),
            "checked_at": time.time(),
        }
        timing = dict(
            getattr(
                state, "wstrade_live_causal_revalidation_detail", {}
            ) or {}
        )
        _replace_execution_contract(
            result, timing.get("execution_contract") or {},
        )
        if event_callback:
            event_callback(
                "LIVE_ENTRY_SUBMIT_REVALIDATED",
                {
                    "ok": causal_ok,
                    "reason": causal_reason,
                    "decision_to_submit_ms": timing.get(
                        "decision_to_submit_ms"
                    ),
                    "flow_state_at_GO": timing.get("flow_state_at_GO"),
                    "flow_state_at_submit": timing.get(
                        "flow_state_at_submit"
                    ),
                    "cash_age_at_submit": timing.get(
                        "cash_age_at_submit"
                    ),
                    "flow_decayed_before_submit": timing.get(
                        "flow_decayed_before_submit", False
                    ),
                    "authority_proof_hash": result.get(
                        "authority_proof_hash"
                    ),
                    "causal_origin_proof": dict(
                        (result.get("authority_dependencies") or {}).get(
                            "causal_origin_proof"
                        ) or {}
                    ),
                    "current_execution_proof": dict(
                        (result.get("authority_dependencies") or {}).get(
                            "current_execution_proof"
                        ) or {}
                    ),
                    "authority_contracts": dict(
                        result.get("authority_contracts") or {}
                    ),
                },
            )
        if not causal_ok:
            return None
        cost_ok, cost_reason, cost_detail = (
            verified_cost_model.validate_execution_cost_contract(
                result,
                state,
                str(result.get("execution_policy") or "MAKER"),
            )
        )
        state.wstrade_live_last_cost_revalidation = {
            "ok": cost_ok,
            "reason": cost_reason,
            "detail": cost_detail,
            "checked_at": time.time(),
        }
        if not cost_ok:
            return None
    qty = mainnet_safety.fixed_quantity()
    if "edge_tier" not in result:
        result["edge_tier"] = dict(
            getattr(state, "entry_edge_tier", {}) or {}
        )
    intent_id = _client_id(state, "intent", side, now)
    transaction = execution_transaction.begin(
        state,
        intent_id=intent_id,
        side=side,
        quantity=qty,
        metadata={
            "causal_episode_id": result.get("causal_episode_id"),
            "canonical_opportunity_id": result.get(
                "canonical_opportunity_id"
            ),
            "decision_cycle_id": result.get("decision_cycle_id"),
            "execution_policy": result.get("execution_policy"),
        },
    )
    if str(transaction.get("transaction_id")) != intent_id:
        _seal_live(state, "ACTIVE_EXECUTION_TRANSACTION", recovery=True)
        _transaction_event(state, event_callback)
        return None
    _transaction_event(state, event_callback)
    try:
        _checkpoint_runtime(state)
    except Exception as exc:
        state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
        state.shadow_persistence_dirty = True
        _seal_live(state, "EXECUTION_INTENT_CHECKPOINT_FAILED", recovery=True)
        _transition_transaction(
            state, "RECOVERY_REQUIRED", event_callback,
            reason="EXECUTION_INTENT_CHECKPOINT_FAILED_BEFORE_SUBMIT",
        )
        return None
    _transition_transaction(
        state, "ORDER_SENT", event_callback,
        execution_policy=str(result.get("execution_policy") or "MAKER").upper(),
        decision_to_submit_ms=_decision_to_submit_ms(result, time.time()),
    )
    try:
        _checkpoint_runtime(state)
    except Exception as exc:
        state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
        state.shadow_persistence_dirty = True
        _seal_live(state, "ORDER_INTENT_CHECKPOINT_FAILED", recovery=True)
        _transition_transaction(
            state, "RECOVERY_REQUIRED", event_callback,
            reason="ORDER_NOT_SUBMITTED_CHECKPOINT_FAILED",
        )
        return None
    order, status, client_id = await _hybrid_entry(api, state, side, result, qty, now)
    if status != 599 and (order or {}).get("orderId") is not None:
        _transition_transaction(
            state, "ACK_KNOWN", event_callback,
            client_order_id=client_id,
            order_id=(order or {}).get("orderId"),
            http_status=status,
        )
    if status != 200 or str((order or {}).get("status", "")) not in (
        "FILLED", "FILLED_RECOVERED_FROM_POSITION"
    ):
        partial = _executed_qty(order)
        recovery_required = bool(
            getattr(state, "wstrade_execution_recovery_required", False)
        )
        flatten_order, flatten_status = None, 599
        if partial > 0.0:
            _transition_transaction(
                state, "PARTIAL_FILL_CONFIRMED", event_callback,
                client_order_id=client_id,
                executed_qty=partial,
                exchange_fill_time_ms=(order or {}).get("updateTime"),
            )
            _transition_transaction(
                state, "UNPROTECTED_EXPOSURE", event_callback,
                executed_qty=partial,
            )
            try:
                _checkpoint_runtime(state)
            except Exception as exc:
                state.wstrade_live_checkpoint_error = (
                    f"{type(exc).__name__}: {exc}"
                )
                state.shadow_persistence_dirty = True
                _seal_live(
                    state, "PARTIAL_FILL_CHECKPOINT_FAILED", recovery=True
                )
        elif recovery_required:
            _transition_transaction(
                state, "EXECUTION_UNKNOWN", event_callback,
                client_order_id=client_id, http_status=status,
            )
            _transition_transaction(
                state, "RECOVERY_REQUIRED", event_callback,
                reason="ENTRY_RESULT_UNVERIFIED",
            )
            try:
                _checkpoint_runtime(state)
            except Exception as exc:
                state.wstrade_live_checkpoint_error = (
                    f"{type(exc).__name__}: {exc}"
                )
                state.shadow_persistence_dirty = True
        else:
            _transition_transaction(
                state, "NO_POSITION", event_callback,
                client_order_id=client_id, http_status=status,
                terminal_status=str((order or {}).get("status") or "UNKNOWN"),
            )
        if partial > 0.0 and not recovery_required:
            flatten_order, flatten_status = await _emergency_flatten(
                api, state, side, partial,
                event_callback=event_callback,
                transaction_reason="PARTIAL_ENTRY_TERMINAL",
            )
        if partial > 0.0:
            state.wstrade_live_last_partial_fill = {
                "side": side, "executed_qty": partial, "order": order,
                "recovery_required": recovery_required,
            }
            _record_aborted_fill(
                state, result, side, partial, order, flatten_order,
                flatten_status, "PARTIAL_ENTRY_TERMINAL",
                event_callback=event_callback,
            )
        return None
    _transition_transaction(
        state, "FILL_CONFIRMED", event_callback,
        client_order_id=client_id,
        order_id=(order or {}).get("orderId"),
        executed_qty=_executed_qty(order) or qty,
        fill_price=_fill_price(order, price),
        exchange_fill_time_ms=(order or {}).get("updateTime"),
    )
    try:
        _checkpoint_runtime(state)
    except Exception as exc:
        state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
        state.shadow_persistence_dirty = True
        _seal_live(state, "FILL_CHECKPOINT_FAILED", recovery=True)
        flatten_order, flatten_status = await _emergency_flatten(
            api, state, side, qty,
            event_callback=event_callback,
            transaction_reason="FILL_CHECKPOINT_FAILED",
        )
        _record_aborted_fill(
            state, result, side, qty, order, flatten_order, flatten_status,
            "FILL_CHECKPOINT_FAILED", event_callback=event_callback,
        )
        return None
    fill = float((order or {}).get("avgPrice", 0.0) or price)
    hard_sl, risk_plan = _risk_geometry(state, side, fill)
    if not bool(risk_plan.get("eligible", False)):
        state.wstrade_live_last_post_fill_rejection = {
            "side": side,
            "fill_price": fill,
            "risk_plan": dict(risk_plan),
            "client_order_id": client_id,
        }
        flatten_order, flatten_status = await _emergency_flatten(
            api, state, side, qty,
            event_callback=event_callback,
            transaction_reason="POST_FILL_RISK_REJECTED",
        )
        _record_aborted_fill(
            state, result, side, qty, order, flatten_order, flatten_status,
            "POST_FILL_RISK_REJECTED", event_callback=event_callback,
        )
        return None
    position = _position(side, qty, fill, hard_sl, risk_plan, now, client_id, result)
    fill_style = (
        "MARKET" if str(result.get("execution_policy", "MAKER")).upper() == "TAKER"
        else "MAKER_TRADE_THROUGH"
    )
    position.execution_cost_plan = verified_cost_model.shadow_execution_plan(
        result, state, fill_style
    )
    # Publish and durably checkpoint the physical exposure before the first
    # network await that submits protection.  A crash can now be reconciled as
    # known unprotected exposure rather than an apparently flat process.
    state.mainnet_shadow_position = position
    state.mainnet_shadow_position_status = "UNPROTECTED_EXPOSURE"
    state.wstrade_live_position = position
    state.wstrade_unprotected_exposure = True
    state.wstrade_live_entry_allowed = False
    _transition_transaction(
        state, "UNPROTECTED_EXPOSURE", event_callback,
        position_cycle_id=position.position_cycle_id,
        hard_sl=position.hard_sl,
    )
    try:
        _checkpoint_runtime(state)
    except Exception as exc:
        state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
        state.shadow_persistence_dirty = True
        _seal_live(state, "UNPROTECTED_EXPOSURE_CHECKPOINT_FAILED", recovery=True)
        flatten_order, flatten_status = await _emergency_flatten(
            api, state, side, qty,
            event_callback=event_callback,
            transaction_reason="UNPROTECTED_EXPOSURE_CHECKPOINT_FAILED",
        )
        _record_aborted_fill(
            state, result, side, qty, order, flatten_order, flatten_status,
            "UNPROTECTED_EXPOSURE_CHECKPOINT_FAILED",
            event_callback=event_callback,
        )
        return None

    protected, stop_result = await _place_stop(
        api, state, position, event_callback=event_callback
    )
    if not protected:
        flatten_order, flatten_status = await _emergency_flatten(
            api, state, side, qty,
            event_callback=event_callback,
            transaction_reason="HARD_STOP_PLACEMENT_FAILED",
        )
        state.wstrade_live_last_stop_failure = stop_result
        _record_aborted_fill(
            state, result, side, qty, order, flatten_order, flatten_status,
            "HARD_STOP_PLACEMENT_FAILED", event_callback=event_callback,
        )
        return None
    _transition_transaction(
        state, "POSITION_PROTECTED", event_callback,
        position_cycle_id=position.position_cycle_id,
        algo_id=position.hard_sl_algo_id,
    )
    state.mainnet_shadow_position_status = "OPEN"
    state.wstrade_unprotected_exposure = False
    try:
        _checkpoint_runtime(state)
    except Exception as exc:
        state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
        state.shadow_persistence_dirty = True
        _seal_live(state, "LIVE_ENTRY_CHECKPOINT_FAILED", recovery=True)
        flatten_order, flatten_status = await _emergency_flatten(
            api, state, side, qty,
            event_callback=event_callback,
            transaction_reason="LIVE_ENTRY_CHECKPOINT_FAILED",
        )
        _record_aborted_fill(
            state, result, side, qty, order, flatten_order, flatten_status,
            "LIVE_ENTRY_CHECKPOINT_FAILED", event_callback=event_callback,
        )
        return None
    if event_callback:
        ignition = dict(result.get("ignition") or {})
        edge = dict(result.get("edge_tier") or {})
        regime = dict(edge.get("micro_regime") or {})
        event_callback("LIVE_ENTRY", {
            "position_cycle_id": position.position_cycle_id,
            "side": side, "qty_btc": qty, "price": fill,
            "hard_sl": hard_sl, "risk_plan": risk_plan,
            "client_order_id": client_id, "lane": result.get("lane", "CORE"),
            "decision_cycle_id": result.get("decision_cycle_id"),
            "canonical_opportunity_id": result.get("canonical_opportunity_id"),
            "causal_episode_id": result.get("causal_episode_id"),
            "proof_type": ignition.get("proof_type"),
            "proposer": ignition.get("proposer"),
            "regime": regime.get("regime"),
            "entry_mode": edge.get("entry_mode"),
            "edge_class": edge.get("edge_class"),
            "frozen_cost_contract": dict(
                result.get("execution_cost_contract")
                or edge.get("execution_cost_contract") or {}
            ),
            "execution_cost_plan": dict(position.execution_cost_plan or {}),
            "order_commission": _commission_snapshot(state, client_id),
            "entry_causal_thesis": dict(position.entry_causal_thesis or {}),
            "authority_contracts": dict(
                getattr(position, "authority_contracts", {}) or {}
            ),
        })
    return position


async def _close_position_locked(
    api, state, position, reason, now=None, event_callback=None
):
    now = time.time() if now is None else float(now)
    if position is None or not bool(getattr(position, "active", False)):
        return False
    client_id = _client_id(state, "close", position.side, now)
    result, status = await api.new_order(
        SYMBOL, "SELL" if position.side == "LONG" else "BUY", "MARKET",
        float(position.qty), positionSide=position.side, newOrderRespType="RESULT",
        newClientOrderId=client_id,
    )
    if status == 599:
        recovered = await _query_fill(api, client_id, state=state)
        if recovered and str(recovered.get("status", "")) == "FILLED":
            result, status = recovered, 200
    if status != 200 or str((result or {}).get("status", "")) not in (
        "FILLED", "FILLED_RECOVERED_FROM_POSITION"
    ):
        _seal_live(state, "CLOSE_UNVERIFIED", recovery=True)
        _transition_transaction(
            state, "RECOVERY_REQUIRED", event_callback,
            reason="CLOSE_UNVERIFIED", client_order_id=client_id,
            http_status=status,
        )
        return False
    stop_cancel_verified = True
    if position.hard_sl_algo_id is not None:
        try:
            _, cancel_status = await api.cancel_algo_order(position.hard_sl_algo_id)
            stop_cancel_verified = cancel_status == 200
        except Exception:
            stop_cancel_verified = False
    if not stop_cancel_verified:
        _seal_live(state, "ORPHAN_STOP_CANCEL_UNVERIFIED", recovery=True)
        _transition_transaction(
            state, "RECOVERY_REQUIRED", event_callback,
            reason="ORPHAN_STOP_CANCEL_UNVERIFIED",
            client_order_id=client_id,
        )
    else:
        _transition_transaction(
            state, "POSITION_CLOSED", event_callback,
            client_order_id=client_id,
            exit_order_id=(result or {}).get("orderId"),
        )
    position.active = False
    state.mainnet_shadow_position_status = "FLAT"
    state.wstrade_live_position = None
    state.wstrade_unprotected_exposure = False
    _complete_pending_demote(state)
    if (
        bool(getattr(state, "wstrade_live_armed", False))
        and bool(getattr(state, "wstrade_user_stream_ready", False))
        and not bool(getattr(state, "wstrade_execution_recovery_required", False))
    ):
        state.wstrade_live_entry_allowed = True
    try:
        _checkpoint_runtime(state)
    except Exception as exc:
        state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
        state.shadow_persistence_dirty = True
        _seal_live(state, "LIVE_EXIT_CHECKPOINT_FAILED", recovery=True)
    if event_callback:
        exit_price = float((result or {}).get("avgPrice", 0.0) or 0.0)
        direction = 1.0 if position.side == "LONG" else -1.0
        gross_bps = (
            direction * (exit_price - position.entry_price)
            / position.entry_price * 10_000.0
            if position.entry_price > 0.0 and exit_price > 0.0 else None
        )
        cost_plan = dict(getattr(position, "execution_cost_plan", {}) or {})
        modeled_fee_bps = float(cost_plan.get("roundtrip_fee_bps", 0.0) or 0.0)
        entry_commission = _commission_snapshot(
            state, getattr(position, "entry_client_order_id", None)
        )
        exit_commission = _commission_snapshot(state, client_id)
        commission_usdt = sum(
            float((item.get("commission_by_asset_cumulative") or {}).get("USDT", 0.0) or 0.0)
            for item in (entry_commission, exit_commission)
        )
        actual_fee_bps = (
            commission_usdt / (position.entry_price * position.qty) * 10_000.0
            if commission_usdt > 0.0 and position.entry_price > 0.0
            and position.qty > 0.0 else None
        )
        fee_bps = actual_fee_bps if actual_fee_bps is not None else modeled_fee_bps
        event_callback("LIVE_EXIT", {
            "position_cycle_id": position.position_cycle_id,
            "side": position.side, "qty_btc": position.qty,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "reason": reason,
            "client_order_id": client_id,
            "decision_cycle_id": getattr(position, "decision_cycle_id", None),
            "causal_episode_id": getattr(position, "causal_episode_id", None),
            "gross_pnl_bps": round(gross_bps, 6) if gross_bps is not None else None,
            "fee_bps": round(fee_bps, 6),
            "net_pnl_bps": (
                round(gross_bps - fee_bps, 6)
                if gross_bps is not None else None
            ),
            "commission_source": (
                "BINANCE_ORDER_TRADE_UPDATE"
                if actual_fee_bps is not None else "VERIFIED_ACCOUNT_RATE_MODEL"
            ),
            "entry_order_commission": entry_commission,
            "exit_order_commission": exit_commission,
            "execution_cost_plan": cost_plan,
            "entry_causal_thesis": dict(
                getattr(position, "entry_causal_thesis", {}) or {}
            ),
            "authority_contracts": dict(
                getattr(position, "authority_contracts", {}) or {}
            ),
            "time_to_positive_net_seconds": getattr(
                position, "edge_time_to_positive_net_seconds", None
            ),
            "economic_contract_version": (
                getattr(position, "entry_causal_thesis", {}) or {}
            ).get("economic_contract_version"),
        })
    return True


async def _reconcile_locked(api, state, event_callback=None, now=None):
    """Verify the exchange position/stop and enforce daily equity loss."""
    local = getattr(state, "mainnet_shadow_position", None)
    local_active = local is not None and bool(getattr(local, "active", False))
    recovery_required = bool(
        getattr(state, "wstrade_execution_recovery_required", False)
    )
    if not (
        bool(getattr(state, "wstrade_live_armed", False))
        or (local_active and bool(getattr(local, "live", False)))
        or recovery_required
    ):
        return "SHADOW"
    now = time.time() if now is None else float(now)
    positions_result, algos_result, orders_result = await asyncio.gather(
        api.get_positions(SYMBOL), api.get_open_algo_orders(SYMBOL),
        api.get_open_orders(SYMBOL),
    )
    positions, position_status = positions_result
    algos, algo_status = algos_result
    orders, order_status = orders_result
    _control_plane_snapshot(api, state)
    if position_status != 200 or algo_status != 200 or order_status != 200:
        state.wstrade_reconciliation_status = "API_UNVERIFIED"
        if _transaction_state(state) and _transaction_state(
            state
        ) not in execution_transaction.TERMINAL_STATES:
            _transition_transaction(
                state, "RECOVERY_REQUIRED", event_callback,
                reason="RECONCILIATION_API_UNVERIFIED",
            )
        return "API_UNVERIFIED"

    if recovery_required:
        # A timed-out/unknown maker is canceled before any partial position is
        # flattened. This ordering prevents a late maker fill from reopening
        # exposure after the emergency market order.
        if orders:
            _, cancel_status = await api.cancel_all_open_orders(SYMBOL)
            if cancel_status != 200:
                state.wstrade_reconciliation_status = "RECOVERY_CANCEL_UNVERIFIED"
                return "RECOVERY_CANCEL_UNVERIFIED"
            orders, order_status = await api.get_open_orders(SYMBOL)
            if order_status != 200 or orders:
                state.wstrade_reconciliation_status = "RECOVERY_ORDER_STILL_OPEN"
                return "RECOVERY_ORDER_STILL_OPEN"
    active_rows = [row for row in positions if abs(
        float(row.get("positionAmt", 0.0) or 0.0)
   ) > 0.0]
    if not local_active and active_rows:
        # Dedicated account: an unowned exchange position is never adopted.
        flattened = True
        last_flatten_order = None
        for row in active_rows:
            amount = float(row.get("positionAmt", 0.0) or 0.0)
            side = str(row.get("positionSide") or ("LONG" if amount > 0 else "SHORT"))
            flatten_order, status = await _emergency_flatten(
                api, state, side, abs(amount),
                event_callback=event_callback,
                transaction_reason=(
                    "UNOWNED_POSITION_RECOVERY"
                    if _transaction_state(state) else None
                ),
            )
            last_flatten_order = flatten_order
            flattened = flattened and status == 200
        verified, verified_status = await api.get_positions(SYMBOL)
        still_active = verified_status != 200 or any(
            abs(float(row.get("positionAmt", 0.0) or 0.0)) > 0.0
            for row in verified
        )
        state.wstrade_live_armed = False
        state.trading_enabled = False
        if not flattened or still_active:
            _seal_live(state, "UNOWNED_POSITION_FLATTEN_UNVERIFIED", recovery=True)
            state.wstrade_reconciliation_status = "UNOWNED_POSITION_UNRESOLVED"
            return "UNOWNED_POSITION_UNRESOLVED"
        state.wstrade_execution_recovery_required = False
        state.execution_unknown = False
        state.wstrade_unprotected_exposure = False
        _mark_flat_verified(
            state, event_callback, reason="UNOWNED_POSITION_FLATTENED"
        )
        _emit_final_aborted_fill(
            state, event_callback, flatten_order=last_flatten_order
        )
        _finalize_shadow_state(state)
        state.wstrade_reconciliation_status = "UNOWNED_POSITION_FLATTENED"
        return "UNOWNED_POSITION_FLATTENED"
    if not local_active:
        if algos:
            _, cancel_status = await api.cancel_all_algo_orders(SYMBOL)
            if cancel_status != 200:
                state.wstrade_reconciliation_status = "ORPHAN_ALGO_CANCEL_FAILED"
                return "ORPHAN_ALGO_CANCEL_FAILED"
        if recovery_required and bool(
            getattr(state, "shadow_persistence_dirty", False)
        ):
            # Do not clear the recovery latch until the durable snapshot also
            # says FLAT; otherwise a crash could resurrect a stale live position.
            state.wstrade_execution_recovery_required = False
            state.execution_unknown = False
            try:
                _checkpoint_runtime(state)
            except Exception as exc:
                state.wstrade_execution_recovery_required = True
                state.execution_unknown = True
                state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
                state.wstrade_reconciliation_status = "RECOVERY_CHECKPOINT_FAILED"
                return "RECOVERY_CHECKPOINT_FAILED"
            state.shadow_persistence_dirty = False
        if recovery_required:
            state.wstrade_execution_recovery_required = False
            state.execution_unknown = False
            state.wstrade_unprotected_exposure = False
            _mark_flat_verified(
                state, event_callback, reason="RECONCILIATION_VERIFIED_FLAT"
            )
            if not bool(getattr(state, "wstrade_live_armed", False)):
                _finalize_shadow_state(state)
        recovered_fill = bool(
            recovery_required
            and float(
                (getattr(state, "wstrade_live_last_entry_outcome", {}) or {}).get(
                    "executed_qty", 0.0
                ) or 0.0
            ) > 0.0
        )
        if recovered_fill:
            _emit_final_aborted_fill(state, event_callback)
            state.wstrade_reconciliation_status = (
                "RECOVERY_VERIFIED_FLAT_AFTER_FILL"
            )
            return "RECOVERY_VERIFIED_FLAT_AFTER_FILL"
        state.wstrade_reconciliation_status = "FLAT"
        return "FLAT"
    exchange_row = next((row for row in active_rows if str(
        row.get("positionSide", "")
    ).upper() == local.side), None)
    if exchange_row is None:
        if algos:
            _, cancel_status = await api.cancel_all_algo_orders(SYMBOL)
            if cancel_status != 200:
                state.wstrade_reconciliation_status = "EXIT_ORPHAN_ALGO_CANCEL_FAILED"
                return "EXIT_ORPHAN_ALGO_CANCEL_FAILED"
        local.active = False
        state.mainnet_shadow_position_status = "FLAT"
        state.wstrade_live_position = None
        state.wstrade_unprotected_exposure = False
        if _transaction_state(state) == "POSITION_PROTECTED":
            _transition_transaction(
                state, "POSITION_CLOSED", event_callback,
                reason="EXCHANGE_POSITION_CLOSED",
            )
        else:
            _mark_flat_verified(
                state, event_callback, reason="EXCHANGE_POSITION_CLOSED"
            )
        _complete_pending_demote(state)
        state.wstrade_reconciliation_status = "EXCHANGE_CLOSED_POSITION"
        if event_callback:
            event_callback("LIVE_EXCHANGE_EXIT", {"side": local.side})
        return "EXCHANGE_CLOSED_POSITION"
    protected = any(
        str(row.get("algoId", "")) == str(local.hard_sl_algo_id)
        or str(row.get("clientAlgoId", "")) == str(local.hard_sl_client_algo_id)
        for row in algos
    )
    if not protected:
        _, close_status = await _emergency_flatten(
            api, state, local.side,
            abs(float(exchange_row.get("positionAmt", 0.0))),
            event_callback=event_callback,
            transaction_reason="HARD_STOP_MISSING",
        )
        if close_status == 200:
            verified, verified_status = await api.get_positions(SYMBOL)
            still_active = verified_status != 200 or any(
                abs(float(row.get("positionAmt", 0.0) or 0.0)) > 0.0
                for row in verified
            )
            if not still_active:
                local.active = False
                state.mainnet_shadow_position_status = "FLAT"
                state.wstrade_live_position = None
                state.wstrade_unprotected_exposure = False
                state.wstrade_execution_recovery_required = False
                state.execution_unknown = False
                _mark_flat_verified(
                    state, event_callback,
                    reason="HARD_STOP_MISSING_FLATTENED",
                )
                _finalize_shadow_state(state)
                state.wstrade_reconciliation_status = "HARD_STOP_MISSING_FLATTENED"
                return "HARD_STOP_MISSING_FLATTENED"
            _seal_live(
                state, "HARD_STOP_MISSING_FLAT_UNVERIFIED", recovery=True
            )
            state.wstrade_reconciliation_status = "HARD_STOP_MISSING_UNRESOLVED"
            return "HARD_STOP_MISSING_UNRESOLVED"
        _seal_live(
            state, "HARD_STOP_MISSING_FLATTEN_UNVERIFIED", recovery=True
        )
        state.wstrade_reconciliation_status = "HARD_STOP_MISSING_UNRESOLVED"
        return "HARD_STOP_MISSING_UNRESOLVED"
    if recovery_required:
        transaction_state = _transaction_state(state)
        if transaction_state in {"PROTECTION_SENT", "PROTECTION_ACKNOWLEDGED"}:
            _transition_transaction(
                state, "PROTECTION_VERIFIED", event_callback,
                reason="RECONCILIATION_FOUND_OPEN_PROTECTION",
                algo_id=getattr(local, "hard_sl_algo_id", None),
                client_algo_id=getattr(local, "hard_sl_client_algo_id", None),
            )
            transaction_state = _transaction_state(state)
        if transaction_state in {"PROTECTION_VERIFIED", "RECOVERY_REQUIRED"}:
            _transition_transaction(
                state, "POSITION_PROTECTED", event_callback,
                reason="RECONCILIATION_VERIFIED_POSITION_AND_STOP",
                algo_id=getattr(local, "hard_sl_algo_id", None),
            )
        elif transaction_state != "POSITION_PROTECTED":
            _transition_transaction(
                state, "RECOVERY_REQUIRED", event_callback,
                reason="POSITION_AND_STOP_FOUND_WITH_UNRESOLVED_TRANSACTION",
            )
            state.wstrade_reconciliation_status = "TRANSACTION_UNRESOLVED"
            return "TRANSACTION_UNRESOLVED"
        state.wstrade_execution_recovery_required = False
        state.execution_unknown = False
        state.wstrade_unprotected_exposure = False
        try:
            _checkpoint_runtime(state)
        except Exception as exc:
            state.shadow_persistence_dirty = True
            state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
            _seal_live(state, "RECOVERY_CHECKPOINT_FAILED", recovery=True)
            state.wstrade_reconciliation_status = "RECOVERY_CHECKPOINT_FAILED"
            return "RECOVERY_CHECKPOINT_FAILED"
    last_daily = float(getattr(state, "wstrade_daily_gate_checked_at", 0.0) or 0.0)
    if now - last_daily >= 15.0:
        state.wstrade_daily_gate_checked_at = now
        daily_ok, daily_reason, daily = await mainnet_safety.refresh_daily_gate(
            api, state, now=now
        )
        unrealized = sum(float(row.get("unRealizedProfit", 0.0) or 0.0) for row in active_rows)
        equity_day_pnl = float(daily.get("net_income_usdt", 0.0) or 0.0) + unrealized
        state.wstrade_daily_equity_pnl_usdt = equity_day_pnl
        if not daily_ok or equity_day_pnl <= -mainnet_safety.daily_loss_limit():
            closed = await _close_position_locked(
                api, state, local,
                {"decision": "EXIT", "reason": daily_reason if not daily_ok else "DAILY_EQUITY_LOSS_BREAKER"},
                now=now, event_callback=event_callback,
            )
            state.wstrade_daily_locked = True
            state.wstrade_daily_lock_day = mainnet_safety.vn_day_start_ms(now)
            status = "DAILY_LOSS_LOCKED" if closed else "DAILY_LOSS_CLOSE_UNVERIFIED"
            state.wstrade_reconciliation_status = status
            return status
    state.wstrade_reconciliation_status = "PROTECTED"
    return "PROTECTED"


async def open_position(api, state, side, result, now=None, event_callback=None):
    async with _execution_lock(state):
        return await _open_position_locked(
            api, state, side, result, now=now, event_callback=event_callback
        )


async def close_position(api, state, position, reason, now=None, event_callback=None):
    async with _execution_lock(state):
        return await _close_position_locked(
            api, state, position, reason, now=now,
            event_callback=event_callback,
        )


async def reconcile(api, state, event_callback=None, now=None):
    async with _execution_lock(state):
        return await _reconcile_locked(
            api, state, event_callback=event_callback, now=now
        )
