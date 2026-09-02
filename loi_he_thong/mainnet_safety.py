"""Fail-closed controls for the dedicated SMC2026 Binance Mainnet subaccount."""

import asyncio
import hashlib
import json
import math
import os
import tempfile
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loi_he_thong import authority_contracts


SYMBOL = "BTCUSDT"
VN_TZ = timezone(timedelta(hours=7))


def safety_contract(state, causal_episode_id=None, *, has_exposure=False):
    """Snapshot operational safety only; never label a market thesis false."""
    runtime = dict(getattr(state, "mainnet_shadow_health", {}) or {})
    operational = list(runtime.get("operational_blockers") or ())
    if bool(getattr(state, "execution_unknown", False)):
        operational.append("execution_unknown")
    if bool(getattr(state, "wstrade_execution_recovery_required", False)):
        operational.append("execution_recovery_required")
    if bool(getattr(state, "shadow_integrity_fault", False)):
        operational.append("journal_integrity_fault")
    operational = list(dict.fromkeys(str(value) for value in operational))

    required_sources = (
        "spot_price", "coinbase_price", "futures_price",
        "spot_flow", "futures_flow",
    )
    unknown_sources = [
        name for name in required_sources if runtime.get(name) is not True
    ]
    if operational:
        safety_state = "SYSTEM_UNSAFE"
        action = (
            "PRESERVE_EXIT_AND_RECONCILIATION_ONLY"
            if has_exposure else "SEAL_NEW_ENTRY"
        )
        reason = operational[0]
    elif unknown_sources:
        safety_state = "UNKNOWN_SOURCE"
        action = (
            "PRESERVE_EXIT_AND_RECONCILIATION_ONLY"
            if has_exposure else "SEAL_NEW_ENTRY"
        )
        reason = "SOURCE_OBSERVATION_INCOMPLETE"
    else:
        safety_state = "CLEAR"
        action = "SAFETY_CLEAR"
        reason = "NO_OPERATIONAL_SAFETY_BLOCKER"
    return authority_contracts.seal(
        "SAFETY", "MAINNET_SAFETY", causal_episode_id,
        {
            "safety_action": action,
            "safety_state": safety_state,
            "safety_reason": reason,
            "operational_blockers": operational,
            "unknown_sources": unknown_sources,
            "has_exposure": bool(has_exposure),
            "market_thesis_rewritten": False,
        },
    )


def _enabled(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env(primary, legacy, default):
    """Prefer the WStrade contract while retaining legacy test compatibility."""
    value = os.getenv(primary)
    return os.getenv(legacy, default) if value is None else value


def execution_venue():
    return _env("WSTRADE_EXECUTION_VENUE", "SMC_EXECUTION_VENUE", "TESTNET").strip().upper()


def is_mainnet(api=None):
    if api is not None:
        return not bool(getattr(api, "testnet", True))
    return execution_venue() == "MAINNET"


def mainnet_armed():
    return _enabled("SMC_MAINNET_ARMED") and _enabled(
        "SMC_MAINNET_EXCLUSIVE_ACCOUNT"
    )


def fixed_quantity():
    return float(_env("WSTRADE_QTY_BTC", "SMC_FIXED_QTY_BTC", "0.001"))


def leverage():
    return int(_env("WSTRADE_LEVERAGE", "SMC_LEVERAGE", "20"))


def max_entries_per_day():
    return int(_env(
        "WSTRADE_MAX_ENTRIES_PER_DAY", "SMC_MAX_FILLED_ENTRIES_PER_VN_DAY", "8"
    ))


def daily_loss_limit():
    return abs(float(_env(
        "WSTRADE_DAILY_LOSS_USDT", "SMC_DAILY_NET_LOSS_USDT", "0.30"
    )))


def max_planned_loss_usdt():
    return abs(float(_env(
        "WSTRADE_MAX_PLANNED_LOSS_USDT", "SMC_MAX_PLANNED_LOSS_USDT", "0.12"
    )))


def max_consecutive_losses():
    return int(_env(
        "WSTRADE_MAX_CONSECUTIVE_LOSSES", "SMC_MAX_CONSECUTIVE_LOSSES", "2"
    ))


def loss_streak_cooldown_seconds():
    return max(0.0, float(os.getenv(
        "SMC_LOSS_STREAK_COOLDOWN_SECONDS", "36000"
    )))


def maker_min_edge_bps():
    return float(os.getenv("SMC_MAINNET_MAKER_MIN_NET_EDGE_BPS", "2.0"))


def market_min_edge_bps():
    return float(os.getenv("SMC_MAINNET_MARKET_MIN_NET_EDGE_BPS", "6.0"))


def stop_slippage_floor_bps():
    return max(0.0, float(os.getenv(
        "SMC_MAINNET_STOP_SLIPPAGE_FLOOR_BPS", "3.0"
    )))


def reserve_usdt():
    return float(_env(
        "WSTRADE_MARGIN_RESERVE_USDT", "SMC_MAINNET_MARGIN_RESERVE_USDT", "0.50"
    ))


def _safety_state_path(state=None):
    configured = _env(
        "WSTRADE_MAINNET_SAFETY_STATE_PATH", "SMC_MAINNET_SAFETY_STATE_PATH", ""
    ).strip()
    if configured:
        return Path(configured)
    journal_dir = os.getenv(
        "SMC_JOURNAL_DIR", "/home/ubuntu/.local/state/smc2026/mainnet"
    )
    return Path(journal_dir) / "safety_state.json"


def _load_safety_state(state=None):
    try:
        payload = json.loads(_safety_state_path(state).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def _save_safety_state(payload, state=None):
    path = _safety_state_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="mainnet_safety_", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _publish_safety_state(state, payload):
    state.mainnet_loss_streak = int(payload.get("loss_streak", 0) or 0)
    state.mainnet_cooldown_until = float(payload.get("cooldown_until", 0.0) or 0.0)
    state.mainnet_safety_ledger = dict(payload)


def _register_outcome(state, cycle_id, net_pnl_quote, closed_at=None):
    """Persist one bot outcome; duplicate reconciliation cannot grow the streak."""
    payload = _load_safety_state(state)
    cycle_id = str(cycle_id or "")
    recent_ids = [
        str(value) for value in payload.get("recent_cycle_ids", ()) if value
    ]
    if not cycle_id or cycle_id in recent_ids:
        _publish_safety_state(state, payload)
        return payload
    closed_at = float(closed_at or time.time())
    net = float(net_pnl_quote or 0.0)
    streak = int(payload.get("loss_streak", 0) or 0)
    streak = streak + 1 if net < 0.0 else 0
    cooldown_until = float(payload.get("cooldown_until", 0.0) or 0.0)
    if max_consecutive_losses() > 0 and streak >= max_consecutive_losses():
        cooldown_until = max(
            cooldown_until, closed_at + loss_streak_cooldown_seconds()
        )
    payload.update({
        "schema_version": 1,
        "updated_at": time.time(),
        "last_cycle_id": cycle_id,
        "last_closed_at": closed_at,
        "last_net_pnl_quote": net,
        "loss_streak": streak,
        "cooldown_until": cooldown_until,
        "recent_cycle_ids": (recent_ids + [cycle_id])[-64:],
    })
    _save_safety_state(payload, state)
    _publish_safety_state(state, payload)
    return payload


def sync_loss_streak_from_cycles(state):
    """Catch exchange-SL closes after fills are reconciled by the journal."""
    payload = _load_safety_state(state)
    last_closed = float(payload.get("last_closed_at", 0.0) or 0.0)
    last_id = str(payload.get("last_cycle_id") or "")
    recent_ids = {
        str(value) for value in payload.get("recent_cycle_ids", ()) if value
    }
    rows = []
    for cycle in getattr(state, "trade_cycles", {}).values():
        if cycle.get("execution_venue") != "BINANCE_FUTURES_MAINNET":
            continue
        closed_at = float(cycle.get("closed_at", 0.0) or 0.0)
        net = (cycle.get("actual") or {}).get("net_pnl_quote")
        cycle_id = str(cycle.get("position_cycle_id") or "")
        if net is None or not cycle_id or closed_at <= 0.0:
            continue
        if cycle_id not in recent_ids and (
            closed_at > last_closed
            or (closed_at == last_closed and cycle_id != last_id)
        ):
            rows.append((closed_at, cycle_id, float(net)))
    for closed_at, cycle_id, net in sorted(rows):
        payload = _register_outcome(state, cycle_id, net, closed_at)
    _publish_safety_state(state, payload)
    return payload


def register_confirmed_mainnet_close(state, position, result, exit_reference):
    """Immediate conservative streak update before delayed commission sync."""
    if getattr(state, "execution_venue", "") != "BINANCE_FUTURES_MAINNET":
        return {}
    entry = float(
        getattr(position, "execution_entry_price", 0.0)
        or getattr(position, "entry_price", 0.0) or 0.0
    )
    exit_price = float(
        (result or {}).get("avgPrice", 0.0) or exit_reference or 0.0
    )
    qty = float(getattr(position, "initial_qty", 0.0) or 0.0)
    side = str(getattr(position, "side", "") or "")
    if min(entry, exit_price, qty) <= 0.0 or side not in ("LONG", "SHORT"):
        return {}
    gross = (
        (exit_price - entry) * qty
        if side == "LONG" else (entry - exit_price) * qty
    )
    risk_plan = dict(getattr(position, "mainnet_risk_plan", {}) or {})
    fee_quote = float(risk_plan.get("fee_reserve_usdt", 0.0) or 0.0)
    if fee_quote <= 0.0:
        fee_bps = float(getattr(state, "mainnet_worst_roundtrip_fee_bps", 10.0))
        fee_quote = entry * qty * fee_bps / 10000.0
    return _register_outcome(
        state, getattr(position, "position_cycle_id", ""),
        gross - fee_quote, time.time(),
    )


def apply_mainnet_loss_budget(
    levels, side, entry_price, quantity, atr, tick_size, spread_bps,
    entry_fee_bps, exit_fee_bps, entry_slippage_bps=0.0,
    exit_slippage_bps=0.0,
):
    """Bound the exchange Hard SL by planned all-in loss, never by wishful PnL."""
    result = {
        "version": "MAINNET_DEFENSIVE_MICRO_V1",
        "eligible": False,
        "reason": "INVALID_RISK_INPUT",
        "max_planned_loss_usdt": max_planned_loss_usdt(),
    }
    levels = dict(levels or {})
    entry = float(entry_price or 0.0)
    qty = float(quantity or 0.0)
    tick = max(float(tick_size or 0.0), 1e-12)
    atr = max(float(atr or 0.0), tick)
    soft = float(levels.get("soft_sl", 0.0) or 0.0)
    structural_hard = float(levels.get("hard_sl", 0.0) or 0.0)
    if side not in ("LONG", "SHORT") or min(entry, qty, soft, structural_hard) <= 0.0:
        return levels, result
    notional = entry * qty
    fee_bps = max(0.0, float(entry_fee_bps or 0.0)) + max(
        0.0, float(exit_fee_bps or 0.0)
    )
    depth_slippage = abs(float(entry_slippage_bps or 0.0)) + abs(
        float(exit_slippage_bps or 0.0)
    )
    slippage_bps = max(
        stop_slippage_floor_bps(),
        2.0 * max(0.0, float(spread_bps or 0.0)),
        depth_slippage + 1.0,
    )
    fee_reserve = notional * fee_bps / 10000.0
    slippage_reserve = notional * slippage_bps / 10000.0
    price_loss_budget = max_planned_loss_usdt() - fee_reserve - slippage_reserve
    min_gap = max(2.0 * tick, 0.10 * atr)
    if price_loss_budget <= 0.0:
        result.update({
            "reason": "FEES_AND_SLIPPAGE_EXCEED_LOSS_BUDGET",
            "fee_reserve_usdt": fee_reserve,
            "slippage_reserve_usdt": slippage_reserve,
        })
        return levels, result
    max_stop_distance = price_loss_budget / qty
    if side == "LONG":
        budget_hard = math.ceil((entry - max_stop_distance) / tick) * tick
        required_hard = math.floor((soft - min_gap) / tick) * tick
        desired_hard = min(structural_hard, required_hard)
        bounded_hard = max(desired_hard, budget_hard)
        bounded_hard = math.ceil(bounded_hard / tick) * tick
        geometry_ok = bounded_hard < soft - min_gap + 1e-12
    else:
        budget_hard = math.floor((entry + max_stop_distance) / tick) * tick
        required_hard = math.ceil((soft + min_gap) / tick) * tick
        desired_hard = max(structural_hard, required_hard)
        bounded_hard = min(desired_hard, budget_hard)
        bounded_hard = math.floor(bounded_hard / tick) * tick
        geometry_ok = bounded_hard > soft + min_gap - 1e-12
    # Keep the approved stop on the exchange tick.  Binary-float rounding to
    # 12 decimals still produced values such as 62850.700000000004 downstream.
    d_tick = Decimal(str(tick))
    d_price = Decimal(str(bounded_hard))
    d_round = ROUND_CEILING if side == "LONG" else ROUND_FLOOR
    bounded_hard = float(
        (d_price / d_tick).to_integral_value(rounding=d_round) * d_tick
    )
    price_loss = qty * abs(entry - bounded_hard)
    planned = price_loss + fee_reserve + slippage_reserve
    result.update({
        "reason": "PASS" if geometry_ok and planned <= max_planned_loss_usdt() + 1e-9
        else "SOFT_SL_OUTSIDE_SAFE_BUDGET",
        "eligible": bool(
            geometry_ok and planned <= max_planned_loss_usdt() + 1e-9
        ),
        "entry_price": entry,
        "structural_hard_sl": structural_hard,
        "required_hard_sl_for_geometry": required_hard,
        "bounded_hard_sl": bounded_hard,
        "hard_sl_expanded_for_geometry": bool(
            (side == "LONG" and bounded_hard < structural_hard - 1e-12)
            or (side == "SHORT" and bounded_hard > structural_hard + 1e-12)
        ),
        "soft_sl": soft,
        "minimum_hard_soft_gap": min_gap,
        "maximum_stop_distance": max_stop_distance,
        "price_loss_usdt": price_loss,
        "fee_bps": fee_bps,
        "fee_reserve_usdt": fee_reserve,
        "slippage_bps": slippage_bps,
        "slippage_reserve_usdt": slippage_reserve,
        "planned_worst_loss_usdt": planned,
    })
    if result["eligible"]:
        levels["hard_sl"] = bounded_hard
    return levels, result


def credential(name, testnet_env_name):
    """Mainnet secrets may only come from systemd credentials, never dotenv."""
    if execution_venue() != "MAINNET":
        return os.getenv(testnet_env_name, "")
    directory = os.getenv("CREDENTIALS_DIRECTORY", "")
    path = os.path.join(directory, name) if directory else ""
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def validate_static_config(filters):
    venue = execution_venue()
    if venue not in ("TESTNET", "MAINNET"):
        return False, "INVALID_EXECUTION_VENUE"
    if venue != "MAINNET":
        return True, "TESTNET"
    if fixed_quantity() != 0.001:
        return False, "MAINNET_FIXED_QTY_MUST_BE_0_001"
    if leverage() != 20:
        return False, "MAINNET_LEVERAGE_MUST_BE_20"
    if _env("WSTRADE_MARGIN_TYPE", "SMC_MARGIN_TYPE", "ISOLATED").upper() != "ISOLATED":
        return False, "MAINNET_MARGIN_MUST_BE_ISOLATED"
    step = float((filters or {}).get("step_size", 0.0) or 0.0)
    minimum = float((filters or {}).get("min_qty", 0.0) or 0.0)
    if step <= 0.0 or minimum <= 0.0:
        return False, "MAINNET_FILTERS_MISSING"
    units = fixed_quantity() / step
    if abs(units - round(units)) > 1e-9 or fixed_quantity() < minimum:
        return False, "MAINNET_FIXED_QTY_FILTER_REJECTED"
    return True, "PASS"


def fixed_quantity_feasibility(equity_usdt, entry_price, filters):
    """Provisional fixed-lot check shared by Commander and Executor.

    The scorer's percentage is an evidence-strength output.  It cannot be
    compared with Binance's 0.001 BTC lot on the dedicated Mainnet account:
    doing so blocks every signal before the Executor can evaluate actual
    stop-risk and fee economics.  This check only proves filter/margin
    feasibility; exchange state, planned loss and realizable edge remain
    fail-closed in ``exchange_entry_gate`` and the Dynamic Path fee gate.
    """
    equity = float(equity_usdt or 0.0)
    price = float(entry_price or 0.0)
    venue_filters = dict(filters or {})
    static_ok, static_reason = validate_static_config(venue_filters)
    qty = fixed_quantity()
    result = {
        'executable': False,
        'reason': static_reason if not static_ok else 'INVALID_EQUITY_OR_PRICE',
        'quantity': qty,
        'fixed_qty_btc': qty,
        'allocation_unit': 'FIXED_BASE_ASSET_QTY',
        'risk_and_economics_pending_executor': True,
    }
    if not static_ok or equity <= 0.0 or price <= 0.0:
        return result
    min_notional = float(venue_filters.get('min_notional', 0.0) or 0.0)
    max_qty = float(venue_filters.get('max_qty', 0.0) or 0.0)
    notional = qty * price
    if min_notional > 0.0 and notional + 1e-12 < min_notional:
        result['reason'] = 'MAINNET_MIN_NOTIONAL_REJECTED'
        return result
    if max_qty > 0.0 and qty > max_qty + 1e-12:
        result['reason'] = 'MAINNET_MAX_QTY_REJECTED'
        return result
    fee_bps = max(
        0.0, float(os.getenv('SMC_SHADOW_ENTRY_FEE_BPS', '5.0'))
    ) + max(
        0.0, float(os.getenv('SMC_SHADOW_EXIT_FEE_BPS', '5.0'))
    )
    initial_margin = notional / max(leverage(), 1)
    fee_reserve = notional * fee_bps / 10000.0
    provisional_required = initial_margin + fee_reserve + reserve_usdt()
    result.update({
        'notional_usdt': notional,
        'initial_margin_usdt': initial_margin,
        'fee_reserve_usdt': fee_reserve,
        'reserve_usdt': reserve_usdt(),
        'provisional_required_balance_usdt': provisional_required,
        'equity_usdt': equity,
    })
    if equity + 1e-12 < provisional_required:
        result['reason'] = 'MAINNET_PROVISIONAL_MARGIN_INSUFFICIENT'
        return result
    result['executable'] = True
    result['reason'] = 'MAINNET_FIXED_QTY_PRECHECK_PASS'
    return result


def vn_day_start_ms(now=None):
    now = datetime.fromtimestamp(time.time() if now is None else now, VN_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _cycle_consecutive_losses(state, start_seconds):
    rows = []
    for cycle in getattr(state, "trade_cycles", {}).values():
        if float(cycle.get("closed_at", 0.0) or 0.0) < start_seconds:
            continue
        if cycle.get("execution_venue") != "BINANCE_FUTURES_MAINNET":
            continue
        net = (cycle.get("actual") or {}).get("net_pnl_quote")
        if net is None:
            continue
        rows.append((float(cycle.get("closed_at", 0.0) or 0.0), float(net)))
    losses = 0
    for _, net in sorted(rows, reverse=True):
        if net < 0.0:
            losses += 1
        else:
            break
    return losses


async def refresh_daily_gate(api, state, now=None):
    """Use Binance as authority for entry count and net daily account income."""
    safety_ledger = sync_loss_streak_from_cycles(state)
    start_ms = vn_day_start_ms(now)
    orders, order_status = await api.get_all_orders(SYMBOL, start_time=start_ms)
    income, income_status = await api.get_income_history(
        SYMBOL, start_time=start_ms
    )
    if order_status != 200 or income_status != 200:
        return False, "MAINNET_DAILY_LIMIT_UNVERIFIED", {}
    entry_ids = set()
    for order in orders if isinstance(orders, list) else ():
        client_id = str(order.get("clientOrderId", ""))
        executed = float(order.get("executedQty", 0.0) or 0.0)
        if executed > 0.0 and client_id.startswith(
            ("smc_entry_", "smc_passi_", "ws_entry_", "ws_maker_")
        ):
            entry_ids.add(str(order.get("orderId", client_id)))
    net = sum(
        float(row.get("income", 0.0) or 0.0)
        for row in income if isinstance(income, list)
        if row.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")
    )
    consecutive = int(safety_ledger.get("loss_streak", 0) or 0)
    cooldown_until = float(safety_ledger.get("cooldown_until", 0.0) or 0.0)
    observed_now = float(time.time() if now is None else now)
    snapshot = {
        "vn_day_start_ms": start_ms,
        "filled_entries": len(entry_ids),
        "net_income_usdt": net,
        "consecutive_losses": consecutive,
        "cooldown_until": cooldown_until,
        "cooldown_remaining_seconds": max(0.0, cooldown_until - observed_now),
    }
    state.mainnet_daily_safety = snapshot
    if max_entries_per_day() > 0 and len(entry_ids) >= max_entries_per_day():
        return False, "MAINNET_DAILY_ENTRY_CAP", snapshot
    if net <= -daily_loss_limit():
        return False, "MAINNET_DAILY_LOSS_BREAKER", snapshot
    if max_consecutive_losses() > 0 and observed_now < cooldown_until:
        return False, "MAINNET_LOSS_STREAK_COOLDOWN", snapshot
    return True, "PASS", snapshot


async def exchange_entry_gate(api, state, entry_price, hard_sl, risk_plan=None):
    if not is_mainnet(api):
        return True, "TESTNET", {}
    if not mainnet_armed():
        return False, "MAINNET_NOT_ARMED", {}
    daily_ok, reason, daily = await refresh_daily_gate(api, state)
    if not daily_ok:
        return False, reason, daily
    positions, position_status = await api.get_positions()
    orders, order_status = await api.get_open_orders()
    algos, algo_status = await api.get_open_algo_orders()
    balance, balance_status = await api.get_balance_details()
    if any(
        status != 200 for status in (
            position_status, order_status, algo_status, balance_status
        )
    ):
        return False, "MAINNET_ACCOUNT_PREFLIGHT_UNVERIFIED", daily
    active = [
        row for row in positions if abs(float(row.get("positionAmt", 0.0) or 0.0)) > 0.0
    ]
    contaminated = any(
        str(row.get("symbol", SYMBOL)) != SYMBOL for row in active
    ) or any(
        str(row.get("symbol", SYMBOL)) != SYMBOL for row in orders
    ) or any(
        str(row.get("symbol", SYMBOL)) != SYMBOL for row in algos
    )
    if contaminated:
        return False, "MAINNET_EXCLUSIVE_ACCOUNT_CONTAMINATED", daily
    if active or orders or algos:
        return False, "MAINNET_ACCOUNT_NOT_FLAT", daily
    qty = fixed_quantity()
    price = float(entry_price or 0.0)
    stop = float(hard_sl or 0.0)
    filters = getattr(state, "exchange_filters", {}) or {}
    static_ok, static_reason = validate_static_config(filters)
    min_notional = float(filters.get("min_notional", 0.0) or 0.0)
    max_qty = float(filters.get("max_qty", 0.0) or 0.0)
    if not static_ok:
        return False, static_reason, daily
    if price <= 0.0 or stop <= 0.0:
        return False, "MAINNET_ENTRY_GEOMETRY_MISSING", daily
    risk_plan = dict(risk_plan or {})
    if not risk_plan.get("eligible"):
        return False, "MAINNET_PLANNED_RISK_UNVERIFIED", {**daily, **risk_plan}
    planned_loss = float(risk_plan.get("planned_worst_loss_usdt", 0.0) or 0.0)
    if planned_loss <= 0.0 or planned_loss > max_planned_loss_usdt() + 1e-9:
        return False, "MAINNET_PLANNED_LOSS_EXCEEDED", {**daily, **risk_plan}
    daily_room = max(0.0, daily_loss_limit() + float(daily.get("net_income_usdt", 0.0)))
    if planned_loss > daily_room + 1e-9:
        return False, "MAINNET_DAILY_RISK_BUDGET_INSUFFICIENT", {
            **daily, **risk_plan, "daily_risk_room_usdt": daily_room,
        }
    if min_notional > 0.0 and qty * price + 1e-12 < min_notional:
        return False, "MAINNET_MIN_NOTIONAL_REJECTED", daily
    if max_qty > 0.0 and qty > max_qty + 1e-12:
        return False, "MAINNET_MAX_QTY_REJECTED", daily
    available = float(balance.get("availableBalance", 0.0) or 0.0)
    notional = qty * price
    initial_margin = notional / leverage()
    stop_loss = qty * abs(price - stop)
    fee_bps = float(getattr(state, "mainnet_worst_roundtrip_fee_bps", 8.0) or 8.0)
    fee_reserve = notional * fee_bps / 10000.0
    required = initial_margin + stop_loss + fee_reserve + reserve_usdt()
    details = {
        **daily, "available_balance": available,
        "required_balance": required, "initial_margin": initial_margin,
        "stop_loss_reserve": stop_loss, "fee_reserve": fee_reserve,
        "daily_risk_room_usdt": daily_room,
        "risk_plan": risk_plan,
    }
    if available + 1e-12 < required:
        return False, "MAINNET_MARGIN_BUFFER_INSUFFICIENT", details
    return True, "PASS", details


def startup_client_id(run_id, side):
    digest = hashlib.sha256(f"{run_id}|STARTUP_FLATTEN|{side}".encode()).hexdigest()[:20]
    return f"smc_boot_{digest}"[:36]


async def prepare_mainnet_account(api, state):
    """Configure only a verified-flat dedicated account; never adopt/flatten residue."""
    if not is_mainnet(api):
        return True, "TESTNET"
    if not mainnet_armed():
        return False, "MAINNET_NOT_ARMED"
    sync_loss_streak_from_cycles(state)
    all_positions, positions_status = await api.get_positions()
    all_orders, orders_status = await api.get_open_orders()
    all_algos, algos_status = await api.get_open_algo_orders()
    if any(status != 200 for status in (
        positions_status, orders_status, algos_status
    )):
        return False, "MAINNET_EXCLUSIVE_ACCOUNT_UNVERIFIED"
    active = [
        row for row in all_positions
        if abs(float(row.get("positionAmt", 0.0) or 0.0)) > 0.0
    ]
    if active or all_orders or all_algos:
        return False, "MAINNET_ACCOUNT_NOT_FLAT"
    multi, status = await api.get_multi_asset_mode()
    if status != 200 or not isinstance(multi, dict):
        return False, "MAINNET_MULTI_ASSET_MODE_UNVERIFIED"
    if bool(multi.get("multiAssetsMargin")):
        changed, change_status = await api.change_multi_asset_mode(False)
        if change_status != 200:
            return False, "MAINNET_MULTI_ASSET_DISABLE_FAILED"
        for _ in range(5):
            await asyncio.sleep(0.20)
            multi, status = await api.get_multi_asset_mode()
            if status == 200 and not bool(multi.get("multiAssetsMargin")):
                break
        else:
            return False, "MAINNET_MULTI_ASSET_DISABLE_UNVERIFIED"
    mode, status = await api.change_position_mode(True)
    code = mode.get("code") if isinstance(mode, dict) else None
    if status != 200 and code != -4059:
        return False, "MAINNET_HEDGE_MODE_FAILED"
    margin, status = await api.change_margin_type(SYMBOL, "ISOLATED")
    code = margin.get("code") if isinstance(margin, dict) else None
    if status != 200 and code != -4046:
        return False, "MAINNET_ISOLATED_MARGIN_FAILED"
    lev, status = await api.change_leverage(SYMBOL, leverage())
    if status != 200 or int((lev or {}).get("leverage", 0) or 0) != leverage():
        return False, "MAINNET_LEVERAGE_FAILED"
    fees, status = await api.get_commission_rate(SYMBOL)
    if status != 200:
        return False, "MAINNET_COMMISSION_UNVERIFIED"
    maker_bps = float(fees.get("makerCommissionRate", 0.0) or 0.0) * 10000.0
    taker_bps = float(fees.get("takerCommissionRate", 0.0) or 0.0) * 10000.0
    configured_maker = float(os.getenv("SMC_PASSIVE_ENTRY_FEE_BPS", "2.0"))
    configured_taker = max(
        float(os.getenv("SMC_SHADOW_ENTRY_FEE_BPS", "5.0")),
        float(os.getenv("SMC_SHADOW_EXIT_FEE_BPS", "5.0")),
    )
    if (
        maker_bps <= 0.0 or taker_bps <= 0.0
        or maker_bps > configured_maker + 1e-9
        or taker_bps > configured_taker + 1e-9
    ):
        return False, "MAINNET_COMMISSION_EXCEEDS_FEE_GATE"
    state.mainnet_maker_fee_bps = maker_bps
    state.mainnet_taker_fee_bps = taker_bps
    state.mainnet_worst_roundtrip_fee_bps = 2.0 * taker_bps
    state.mainnet_commission_verified = True
    state.mainnet_commission_source = "BINANCE_ACCOUNT_COMMISSION_RATE"

    # Re-read after configuration to close the race between initial validation
    # and arming. Any new state is contamination, never something to auto-close.
    all_positions, positions_status = await api.get_positions()
    all_orders, orders_status = await api.get_open_orders()
    all_algos, algos_status = await api.get_open_algo_orders()
    if any(status != 200 for status in (
        positions_status, orders_status, algos_status
    )):
        return False, "MAINNET_EXCLUSIVE_ACCOUNT_UNVERIFIED"
    active = [
        row for row in all_positions
        if abs(float(row.get("positionAmt", 0.0) or 0.0)) > 0.0
    ]
    if active or all_orders or all_algos:
        return False, "MAINNET_ACCOUNT_NOT_FLAT"
    positions, status = await api.get_positions(SYMBOL)
    if status != 200:
        return False, "MAINNET_STARTUP_POSITION_UNVERIFIED"
    for row in positions:
        margin_type = row.get("marginType")
        row_leverage = row.get("leverage")
        # Binance Position Risk may omit these configuration fields.  A
        # present value must match; absent values are covered by the successful
        # configuration calls above rather than treated as proof of mismatch.
        if margin_type is not None and str(margin_type).lower() != "isolated":
            return False, "MAINNET_MARGIN_VERIFICATION_FAILED"
        if row_leverage is not None and int(float(row_leverage)) != leverage():
            return False, "MAINNET_LEVERAGE_VERIFICATION_FAILED"
    return True, "PASS"
