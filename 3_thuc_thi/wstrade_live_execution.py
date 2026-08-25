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

from loi_he_thong import mainnet_safety
from loi_he_thong import private_user_stream
from loi_he_thong import verified_cost_model


VERSION = "WSTRADE_LIVE_EXECUTION_V1"
SYMBOL = "BTCUSDT"
MAKER_TTL_SECONDS = 0.75
CAUSAL_SUBMIT_MAX_AGE_SECONDS = 1.5
BBO_SUBMIT_MAX_AGE_SECONDS = 1.0


def _revalidate_before_submit(state, side, result, now=None):
    """Fail closed if REST preflight outlived the recorded causal decision."""
    now = time.time() if now is None else float(now)
    side = str(side or "").upper()
    if side not in ("LONG", "SHORT"):
        return False, "SIDE_INVALID"
    if str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper() != side:
        return False, "BIAS_SIDE_CHANGED"
    if float(getattr(state, "bias_confidence", 0.0) or 0.0) < 0.55:
        return False, "BIAS_CONFIDENCE_DROPPED"
    decision_ts = float((result or {}).get("ts", 0.0) or 0.0)
    decision_age = now - decision_ts
    if (
        decision_ts <= 0.0 or decision_age < 0.0
        or decision_age > CAUSAL_SUBMIT_MAX_AGE_SECONDS
    ):
        return False, "CAUSAL_PROOF_STALE"
    bid = float(getattr(state, "execution_best_bid", 0.0) or 0.0)
    ask = float(getattr(state, "execution_best_ask", 0.0) or 0.0)
    bbo_ts = float(getattr(state, "execution_price_time", 0.0) or 0.0)
    bbo_age = now - bbo_ts
    if bid <= 0.0 or ask <= bid:
        return False, "BBO_INVALID"
    if bbo_ts <= 0.0 or bbo_age < 0.0 or bbo_age > BBO_SUBMIT_MAX_AGE_SECONDS:
        return False, "BBO_STALE"
    ignition = (result or {}).get("ignition") or {}
    if ignition.get("futures_follow_invalidated"):
        return False, "FUTURES_FOLLOW_INVALIDATED"
    return True, "PASS"


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
            state.wstrade_live_arm_reason = "MAINNET_BALANCE_UNAVAILABLE"
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
    release = str(result.get("phase", "")).upper() == "RELEASE"
    edge_ok = bool((result.get("edge_tier") or {}).get("cost_ok", False))
    if release and edge_ok:
        return await _market_entry(api, state, side, qty, time.time())
    return final or placed, 409, client_id


async def _place_stop(api, state, position):
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
    response, status = await api.new_algo_order(**params)
    if status == 200 and (response or {}).get("algoId") is not None:
        position.hard_sl_algo_id = response.get("algoId")
        position.hard_sl_client_algo_id = response.get("clientAlgoId", client_id)
        return True, response
    open_algos, open_status = await api.get_open_algo_orders(SYMBOL)
    recovered = next(
        (row for row in open_algos if row.get("clientAlgoId") == client_id), None
    ) if open_status == 200 and isinstance(open_algos, list) else None
    if recovered:
        position.hard_sl_algo_id = recovered.get("algoId")
        position.hard_sl_client_algo_id = client_id
        return True, recovered
    return False, response


async def _emergency_flatten(api, state, side, qty):
    client_id = _client_id(state, "panic", side, time.time())
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
        status = 599
    return result, status


def _entry_causal_thesis(result):
    """Keep only the cash-led facts Guardian needs to judge thesis failure."""
    causal=(result or {}).get("causal") or {}
    ignition=(result or {}).get("ignition") or causal.get("ignition") or {}
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
        return {
            "version":"IGNITION_CAUSAL_THESIS_V1",
            "primary_cash_anchor":primary,
            "cash_anchors":sorted(cash_aliases),
            "handoff_status":str(ignition.get("leader") or "UNKNOWN"),
            "oi_intent":dict(ignition.get("oi_intent") or {}),
            "proof_type":ignition.get("proof_type"),
            "proposer":ignition.get("proposer"),
            "impulse_phase":ignition.get("impulse_phase"),
            "residual_edge_proxy_bps":ignition.get("residual_edge_proxy_bps"),
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
        entry_causal_thesis=_entry_causal_thesis(result),
    )


async def open_position(api, state, side, result, now=None, event_callback=None):
    now = time.time() if now is None else float(now)
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
    if (
        bool(getattr(state, "wstrade_live_armed", False))
        and int((result or {}).get("canonical_opportunity_id", 0) or 0) > 0
    ):
        causal_ok, causal_reason = _revalidate_before_submit(
            state, side, result, now=time.time(),
        )
        state.wstrade_live_last_causal_revalidation = {
            "ok": causal_ok, "reason": causal_reason, "checked_at": time.time(),
        }
        if not causal_ok:
            return None
    qty = mainnet_safety.fixed_quantity()
    result = dict(result or {})
    if "edge_tier" not in result:
        result["edge_tier"] = dict(
            getattr(state, "entry_edge_tier", {}) or {}
        )
    order, status, client_id = await _hybrid_entry(api, state, side, result, qty, now)
    if status != 200 or str((order or {}).get("status", "")) not in (
        "FILLED", "FILLED_RECOVERED_FROM_POSITION"
    ):
        partial = _executed_qty(order)
        recovery_required = bool(
            getattr(state, "wstrade_execution_recovery_required", False)
        )
        if partial > 0.0 and not recovery_required:
            await _emergency_flatten(api, state, side, partial)
        if partial > 0.0:
            state.wstrade_live_last_partial_fill = {
                "side": side, "executed_qty": partial, "order": order,
                "recovery_required": recovery_required,
            }
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
        _, flatten_status = await _emergency_flatten(api, state, side, qty)
        if event_callback:
            event_callback("LIVE_POST_FILL_RISK_REJECTED", {
                "side": side,
                "qty_btc": qty,
                "fill_price": fill,
                "risk_plan": risk_plan,
                "flatten_verified": flatten_status == 200,
            })
        return None
    position = _position(side, qty, fill, hard_sl, risk_plan, now, client_id, result)
    fill_style = (
        "MARKET" if str(result.get("execution_policy", "MAKER")).upper() == "TAKER"
        else "MAKER_TRADE_THROUGH"
    )
    position.execution_cost_plan = verified_cost_model.shadow_execution_plan(
        result, state, fill_style
    )
    protected, stop_result = await _place_stop(api, state, position)
    if not protected:
        await _emergency_flatten(api, state, side, qty)
        state.wstrade_live_last_stop_failure = stop_result
        return None
    state.mainnet_shadow_position = position
    state.mainnet_shadow_position_status = "OPEN"
    state.wstrade_live_position = position
    try:
        _checkpoint_runtime(state)
    except Exception as exc:
        state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
        state.shadow_persistence_dirty = True
        _seal_live(state, "LIVE_ENTRY_CHECKPOINT_FAILED", recovery=True)
        _, flatten_status = await _emergency_flatten(api, state, side, qty)
        if flatten_status == 200:
            if position.hard_sl_algo_id is not None:
                try:
                    await api.cancel_algo_order(position.hard_sl_algo_id)
                except Exception:
                    pass
            position.active = False
            state.mainnet_shadow_position_status = "FLAT"
            state.wstrade_live_position = None
        return None
    if event_callback:
        event_callback("LIVE_ENTRY", {
            "side": side, "qty_btc": qty, "price": fill,
            "hard_sl": hard_sl, "risk_plan": risk_plan,
            "client_order_id": client_id, "lane": result.get("lane", "CORE"),
            "decision_cycle_id": result.get("decision_cycle_id"),
            "canonical_opportunity_id": result.get("canonical_opportunity_id"),
            "causal_episode_id": result.get("causal_episode_id"),
        })
    return position


async def close_position(api, state, position, reason, now=None, event_callback=None):
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
    position.active = False
    state.mainnet_shadow_position_status = "FLAT"
    state.wstrade_live_position = None
    _complete_pending_demote(state)
    try:
        _checkpoint_runtime(state)
    except Exception as exc:
        state.wstrade_live_checkpoint_error = f"{type(exc).__name__}: {exc}"
        state.shadow_persistence_dirty = True
        _seal_live(state, "LIVE_EXIT_CHECKPOINT_FAILED", recovery=True)
    if event_callback:
        event_callback("LIVE_EXIT", {
            "side": position.side, "qty_btc": position.qty,
            "entry_price": position.entry_price,
            "exit_price": float((result or {}).get("avgPrice", 0.0) or 0.0),
            "reason": reason,
            "decision_cycle_id": getattr(position, "decision_cycle_id", None),
            "causal_episode_id": getattr(position, "causal_episode_id", None),
        })
    return True


async def reconcile(api, state, event_callback=None, now=None):
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
    if position_status != 200 or algo_status != 200 or order_status != 200:
        state.wstrade_reconciliation_status = "API_UNVERIFIED"
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
        for row in active_rows:
            amount = float(row.get("positionAmt", 0.0) or 0.0)
            side = str(row.get("positionSide") or ("LONG" if amount > 0 else "SHORT"))
            _, status = await _emergency_flatten(api, state, side, abs(amount))
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
            if not bool(getattr(state, "wstrade_live_armed", False)):
                _finalize_shadow_state(state)
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
            api, state, local.side, abs(float(exchange_row.get("positionAmt", 0.0)))
        )
        if close_status == 200:
            local.active = False
            state.mainnet_shadow_position_status = "FLAT"
            state.wstrade_live_position = None
            _finalize_shadow_state(state)
            state.wstrade_reconciliation_status = "HARD_STOP_MISSING_FLATTENED"
            return "HARD_STOP_MISSING_FLATTENED"
        else:
            _seal_live(state, "HARD_STOP_MISSING_FLATTEN_UNVERIFIED", recovery=True)
            state.wstrade_reconciliation_status = "HARD_STOP_MISSING_UNRESOLVED"
            return "HARD_STOP_MISSING_UNRESOLVED"

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
            closed = await close_position(
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
