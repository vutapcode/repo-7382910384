"""Bounded Binance forceOrder context for shadow-entry research.

Liquidations are derivative closing flow, not independent direction evidence.
This module therefore cannot create an entry.  It can only identify a likely
late liquidation tail when cash independence is weak and refreshed OI agrees
that positions are being closed.
"""

from collections import deque
from statistics import median


VERSION = "LIQUIDATION_CONTEXT_SHADOW_V1"
RETENTION_SECONDS = 65
DEDUP_SIZE = 256


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ensure(state):
    rows = getattr(state, "liquidation_flow_1s", None)
    if rows is None or getattr(rows, "maxlen", None) != RETENTION_SECONDS:
        rows = deque(list(rows or ())[-RETENTION_SECONDS:], maxlen=RETENTION_SECONDS)
        state.liquidation_flow_1s = rows
    tokens = getattr(state, "liquidation_seen_tokens", None)
    if tokens is None or getattr(tokens, "maxlen", None) != DEDUP_SIZE:
        tokens = deque(list(tokens or ())[-DEDUP_SIZE:], maxlen=DEDUP_SIZE)
        state.liquidation_seen_tokens = tokens
        state.liquidation_seen_token_set = set(tokens)
    token_set = getattr(state, "liquidation_seen_token_set", None)
    if not isinstance(token_set, set):
        token_set = set(tokens)
        state.liquidation_seen_token_set = token_set
    return rows, tokens, token_set


def reset_epoch(state):
    rows, tokens, token_set = _ensure(state)
    rows.clear()
    tokens.clear()
    token_set.clear()
    state.liquidation_last_receive_ms = 0.0
    state.liquidation_epoch = int(getattr(state, "liquidation_epoch", 0) or 0) + 1


def _remember(tokens, token_set, token):
    if token in token_set:
        return False
    if len(tokens) >= tokens.maxlen:
        token_set.discard(tokens[0])
    tokens.append(token)
    token_set.add(token)
    return True


def observe_force_order(state, payload, receive_ms):
    """Apply one forceOrder without mixing it into canonical aggTrade flow."""
    data = dict(payload or {})
    nested = data.get("data")
    if isinstance(nested, dict):
        data = nested
    if str(data.get("e") or "") != "forceOrder":
        return "IGNORED"
    order = data.get("o") or {}
    side = str(order.get("S") or "").upper()
    price = _f(order.get("ap") or order.get("p"))
    qty = _f(order.get("z") or order.get("q"))
    receive_ms = _f(receive_ms)
    event_ms = int(_f(data.get("E") or order.get("T") or receive_ms))
    if side not in ("BUY", "SELL") or price <= 0.0 or qty <= 0.0 or receive_ms <= 0.0:
        return "INVALID"
    last_receive = _f(getattr(state, "liquidation_last_receive_ms", 0.0))
    if last_receive and receive_ms + 1.0 < last_receive:
        return "OUT_OF_ORDER"
    rows, tokens, token_set = _ensure(state)
    token = (str(order.get("i") or ""), event_ms, side, round(price, 2), round(qty, 6))
    if not _remember(tokens, token_set, token):
        return "DUPLICATE"

    second = int(receive_ms // 1000.0)
    if rows and int(rows[-1]["second"]) > second:
        return "OUT_OF_ORDER"
    if not rows or int(rows[-1]["second"]) != second:
        rows.append({
            "second": second, "long_quote": 0.0, "short_quote": 0.0,
            "long_count": 0, "short_count": 0,
        })
    # SELL liquidates a long; BUY liquidates/covers a short.
    kind = "long" if side == "SELL" else "short"
    quote = price * qty
    rows[-1][f"{kind}_quote"] += quote
    rows[-1][f"{kind}_count"] += 1
    state.liquidation_last_receive_ms = receive_ms
    state.long_liquidation_quote_total = _f(
        getattr(state, "long_liquidation_quote_total", 0.0)
    ) + (quote if kind == "long" else 0.0)
    state.short_liquidation_quote_total = _f(
        getattr(state, "short_liquidation_quote_total", 0.0)
    ) + (quote if kind == "short" else 0.0)
    events = getattr(state, "liquidation_events", None)
    if events is None:
        events = deque(maxlen=128)
        state.liquidation_events = events
    events.append({
        "event_time_ms": event_ms, "receive_time_ms": int(receive_ms),
        "liquidated_side": kind.upper(), "quote": quote,
        "price": price, "qty": qty,
    })
    return "LIQUIDATION"


def _window(rows, now_second, seconds, key):
    cutoff = now_second - seconds + 1
    selected = [row for row in rows if int(row["second"]) >= cutoff]
    selected = [row for row in selected if int(row["second"]) <= now_second]
    return (
        sum(_f(row.get(f"{key}_quote")) for row in selected),
        sum(int(row.get(f"{key}_count", 0) or 0) for row in selected),
    )


def snapshot(state, side, now):
    rows, _, _ = _ensure(state)
    now_second = int(_f(now) or 0.0)
    kind = "short" if str(side).upper() == "LONG" else "long"
    one_quote, one_count = _window(rows, now_second, 1, kind)
    three_quote, three_count = _window(rows, now_second, 3, kind)
    fifteen_quote, fifteen_count = _window(rows, now_second, 15, kind)
    sixty_quote, sixty_count = _window(rows, now_second, 60, kind)
    history = [
        _f(row.get(f"{kind}_quote")) for row in rows
        if now_second - 60 < int(row["second"]) <= now_second - 3
    ]
    base = median(history) if history else 0.0
    mad = median([abs(value - base) for value in history]) if history else 0.0
    burst_floor = max(25_000.0, 3.0 * (base + 3.0 * mad))
    burst = bool(three_count > 0 and three_quote >= burst_floor)
    previous_two = max(0.0, three_quote - one_quote)
    decelerating = bool(burst and previous_two > 0.0 and one_quote * 2.0 < previous_two * 0.5)
    if not rows:
        phase = "UNKNOWN"
    elif decelerating:
        phase = "DECELERATING"
    elif burst and one_quote > 0.0:
        phase = "CASCADE"
    elif fifteen_quote > 0.0:
        phase = "BUILDUP"
    else:
        phase = "QUIET"
    return {
        "version": VERSION, "authority": "SHADOW_ONLY",
        "side": str(side).upper(), "same_direction_closing_kind": kind.upper(),
        "phase": phase, "burst": burst, "decelerating": decelerating,
        "quote_1s": round(one_quote, 6), "quote_3s": round(three_quote, 6),
        "quote_15s": round(fifteen_quote, 6), "quote_60s": round(sixty_quote, 6),
        "count_1s": one_count, "count_3s": three_count,
        "count_15s": fifteen_count, "count_60s": sixty_count,
        "baseline_quote_1s": round(base, 6), "baseline_mad_quote_1s": round(mad, 6),
        "burst_floor_quote": round(burst_floor, 6),
        "last_receive_age_seconds": (
            round(max(0.0, _f(now) - _f(getattr(state, "liquidation_last_receive_ms", 0.0)) / 1000.0), 4)
            if _f(getattr(state, "liquidation_last_receive_ms", 0.0)) > 0.0 else None
        ),
    }


def assess_entry(state, result, now):
    """Return a conservative research veto; forceOrder alone is never enough."""
    ignition = (result or {}).get("ignition") or {}
    report = snapshot(state, (result or {}).get("side"), now)
    oi = ignition.get("oi_intent") or {}
    verification = ignition.get("oi_verification_state") or {}
    verification_status = str(
        verification.get("status") or "UNAVAILABLE"
    ).upper()
    cash = set(ignition.get("cash_venues") or ())
    dual_cash = {"binance_spot", "coinbase_spot"}.issubset(cash)
    # Only an OI refresh inside this exact causal episode may corroborate a
    # forced unwind.  A young but unchanged pre-episode snapshot is UNKNOWN,
    # even when the legacy intent metadata says fresh.
    unwind = verification_status == "FRESH_UNWIND"
    tail = bool(report["decelerating"] and unwind and not dual_cash)
    report.update({
        "oi_unwind_confirmed": unwind,
        "oi_verification_status": verification_status,
        "raw_oi_intent": str(oi.get("intent") or "UNKNOWN").upper(),
        "independent_cash_confirmed": dual_cash,
        "tail_veto": tail,
        "reason": "LIQUIDATION_TAIL_COMPOSITE" if tail else "CONTEXT_ONLY",
        "can_create_direction": False,
    })
    return report
