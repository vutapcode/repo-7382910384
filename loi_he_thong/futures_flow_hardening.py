"""Mainnet shadow Futures-flow hardening: local-time windows + saturation fail-closed."""
import asyncio
from collections import deque
import logging
import time

from loi_he_thong import ignition_signals
from loi_he_thong import liquidation_context

RETENTION_MS = 20_000.0
HARD_MAX = 12_000
REQUIRED_COVERAGE_SEC = 15.0
BIAS_BUCKET_RETENTION_SECONDS = 65


def _update_bias_flow_bucket(state, recv_ms, is_sell, quantity):
    """Maintain exact 1s Futures flow buckets for the 60s Bias seat.

    Entry keeps the raw 20s ring for its 3s scans. Bias consumes at most 65
    aggregate rows, so its declared 60s horizon no longer depends on raw-trade
    retention or market event rate.
    """
    rows = getattr(state, "futures_flow_1s_buffer", None)
    if rows is None or getattr(rows, "maxlen", None) != BIAS_BUCKET_RETENTION_SECONDS:
        rows = deque(list(rows or ())[-BIAS_BUCKET_RETENTION_SECONDS:],
                     maxlen=BIAS_BUCKET_RETENTION_SECONDS)
        state.futures_flow_1s_buffer = rows
    second = int(float(recv_ms) // 1000.0)
    if rows and int(rows[-1].get("second", -1)) > second:
        rows.clear()
    if not rows or int(rows[-1].get("second", -1)) != second:
        rows.append({"second": second, "ts": float(second), "buy": 0.0, "sell": 0.0})
    key = "sell" if is_sell else "buy"
    rows[-1][key] += float(quantity)
    state.futures_flow_60s_coverage_sec = (
        max(0.0, float(rows[-1]["second"] - rows[0]["second"])) if len(rows) >= 2 else 0.0
    )
    return rows


def _apply_spot_event(state, data, recv_ms, recv_mono_ns=None):
    """Apply one Binance Spot aggTrade to the canonical CVD input queue."""
    data = dict(data or {})
    try:
        row = {
            "gia": float(data["p"]),
            "khoi_luong": float(data["q"]),
            "ban_chu_dong": bool(data["m"]),
            "thoi_gian_ms": int(recv_ms),
            "exchange_time_ms": int(data.get("E", 0) or 0),
            "receive_time_monotonic_ns": int(
                recv_mono_ns or time.monotonic_ns()
            ),
            "nguon": "SPOT",
        }
    except (KeyError, TypeError, ValueError):
        return "INVALID"
    if row["gia"] <= 0.0 or row["khoi_luong"] <= 0.0:
        return "INVALID"
    state.danh_sach_khop_lenh.append(row)
    state.thoi_gian_dong_tien_cuoi = float(recv_ms) / 1000.0
    ignition_signals.observe_trade(
        state, "binance_spot", receive_time_ms=recv_ms,
        event_time_ms=row["exchange_time_ms"] or recv_ms,
        price=row["gia"], qty=row["khoi_luong"],
        aggressive_buy=not row["ban_chu_dong"],
        receive_time_monotonic_ns=row["receive_time_monotonic_ns"],
        source_health="FRESH",
    )
    return "TRADE"


def _apply_futures_event(state, data, recv_ms, recv_mono_ns=None):
    """Apply one Binance Futures aggTrade to the bounded canonical flow ring."""
    data = dict(data or {})
    event_type = str(data.get("e", ""))
    if event_type not in ("trade", "aggTrade"):
        return "IGNORED"
    row = {
        "gia": float(data["p"]),
        "khoi_luong": float(data["q"]),
        "ban_chu_dong": bool(data["m"]),
        "thoi_gian_ms": int(recv_ms),
        "exchange_time_ms": int(data.get("E", 0) or 0),
        "receive_time_monotonic_ns": int(
            recv_mono_ns or time.monotonic_ns()
        ),
        "nguon": "FUTURES",
    }
    ring = _ensure_ring(state)
    ring.append(row)
    _update_bias_flow_bucket(
        state, recv_ms, row["ban_chu_dong"], row["khoi_luong"]
    )
    _trim(state, recv_ms)
    state.thoi_gian_dong_tien_futures_cuoi = recv_ms / 1000.0
    ignition_signals.observe_trade(
        state, "futures", receive_time_ms=recv_ms,
        event_time_ms=row["exchange_time_ms"] or recv_ms,
        price=row["gia"], qty=row["khoi_luong"],
        aggressive_buy=not row["ban_chu_dong"],
        receive_time_monotonic_ns=row["receive_time_monotonic_ns"],
        source_health="FRESH",
    )
    return "TRADE"


def _unwrap_combined_stream(payload):
    """Return the event carried by Binance's combined market-stream envelope."""
    payload = dict(payload or {})
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _dispatch_futures_payload(
    state, data, recv_ms, recv_mono_ns=None, force_order_observer=None,
):
    """Route one combined-stream event to exactly one causal owner."""
    event = _unwrap_combined_stream(data)
    if str(event.get("e", "")) == "forceOrder":
        observer = (
            force_order_observer
            if callable(force_order_observer)
            else liquidation_context.observe_force_order
        )
        return observer(state, event, recv_ms)
    return _apply_futures_event(state, event, recv_ms, recv_mono_ns)


def _ensure_ring(state):
    ring = getattr(state, "danh_sach_khop_lenh_futures", None)
    maxlen = getattr(ring, "maxlen", None) if ring is not None else None
    if ring is None or maxlen is None or int(maxlen) < HARD_MAX:
        ring = deque(list(ring or ())[-HARD_MAX:], maxlen=HARD_MAX)
        state.danh_sach_khop_lenh_futures = ring
    return ring


def _publish_ring_health(state, ring):
    coverage = 0.0
    if len(ring) >= 2:
        try:
            first = float(ring[0].get("thoi_gian_ms", 0.0) or 0.0)
            last = float(ring[-1].get("thoi_gian_ms", 0.0) or 0.0)
            coverage = max(0.0, (last - first) / 1000.0)
        except (AttributeError, TypeError, ValueError):
            coverage = 0.0
    full = bool(ring.maxlen and len(ring) >= ring.maxlen)
    state.futures_flow_ring_coverage_sec = coverage
    saturated = bool(full and coverage < REQUIRED_COVERAGE_SEC)
    state.futures_flow_ring_saturated = saturated
    state.futures_flow_ring_size = len(ring)
    state.futures_flow_ring_maxlen = ring.maxlen
    if saturated:
        # Fail closed immediately. The canonical shadow health probe may later restore
        # readiness only after the ring is no longer truncated.
        state.mainnet_shadow_ready = False
        state.system_ready = False
        state.last_readiness_reason = "SHADOW_FEED_DEGRADED:futures_flow_ring_saturated"


def _trim(state, now_ms):
    ring = _ensure_ring(state)
    cutoff = float(now_ms) - RETENTION_MS
    while ring:
        try:
            ts = float(ring[0].get("thoi_gian_ms", 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            ring.popleft()
            continue
        if ts >= cutoff:
            break
        ring.popleft()
    _publish_ring_health(state, ring)
    return ring


def install(base):
    """Replace the Spot/Futures aggTrade collectors used by Mainnet shadow runtime."""
    mod = base.app.tai_dong_tien

    def reset_spot_epoch(state):
        for name in ("danh_sach_khop_lenh", "flow_1s_buffer", "trade_flow_timeline"):
            buf = getattr(state, name, None)
            if buf is not None:
                try:
                    buf.clear()
                except (AttributeError, TypeError):
                    pass
        for name in (
            "current_vol_3s",
            "current_cvd_buy_3s",
            "current_cvd_sell_3s",
            "last_3s_window_ts",
            "last_trade_event_time_s",
            "thoi_gian_dong_tien_cuoi",
        ):
            setattr(state, name, 0.0)
        state.spot_flow_epoch = int(getattr(state, "spot_flow_epoch", 0) or 0) + 1
        state.spot_flow_epoch_started_at = time.time()
        ignition_signals.reset_venue(
            state, "binance_spot", state.spot_flow_epoch
        )

    def reset_futures_epoch(state):
        ring = _ensure_ring(state)
        ring.clear()
        bias_rows = getattr(state, "futures_flow_1s_buffer", None)
        if bias_rows is not None:
            bias_rows.clear()
        state.futures_flow_60s_coverage_sec = 0.0
        state.futures_flow_ring_saturated = False
        state.futures_flow_ring_coverage_sec = 0.0
        state.futures_flow_ring_size = 0
        state.thoi_gian_dong_tien_futures_cuoi = 0.0
        state.futures_flow_epoch = int(getattr(state, "futures_flow_epoch", 0) or 0) + 1
        state.futures_flow_epoch_started_at = time.time()
        ignition_signals.reset_venue(
            state, "futures", state.futures_flow_epoch
        )
        return ring

    async def spot_local_time(symbol: str, state):
        url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@aggTrade"
        while True:
            try:
                async with mod.websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    reset_spot_epoch(state)
                    last_agg_id = None
                    logging.info("[SPOT FLOW] hardened local-time collector epoch=%s: %s", state.spot_flow_epoch, symbol.upper())
                    async for raw in ws:
                        try:
                            data = mod.orjson.loads(raw)
                            recv_ms = time.time() * 1000.0
                            recv_mono_ns = time.monotonic_ns()
                            agg_id = int(data.get("a", 0) or 0)
                            if (
                                last_agg_id is not None and agg_id > 0
                                and agg_id != last_agg_id + 1
                            ):
                                logging.warning(
                                    "[SPOT FLOW] sequence gap expected=%s actual=%s; new epoch",
                                    last_agg_id + 1, agg_id,
                                )
                                reset_spot_epoch(state)
                            if _apply_spot_event(
                                state, data, recv_ms, recv_mono_ns
                            ) == "TRADE" and agg_id > 0:
                                last_agg_id = agg_id
                        except (TypeError, ValueError):
                            continue
            except asyncio.CancelledError:
                raise
            except mod.websockets.exceptions.ConnectionClosed as exc:
                logging.warning("[SPOT FLOW] reconnect: %s", exc)
                await asyncio.sleep(3)
            except Exception:
                logging.exception("[SPOT FLOW] hardened collector failure")
                await asyncio.sleep(3)

    async def hardened(
        symbol: str,
        state,
        force_order_observer=None,
        force_order_epoch_reset=None,
    ):
        # The direct /ws endpoint can complete the handshake yet deliver no
        # Futures events from some Lightsail routes.  Keep aggTrade and
        # forceOrder on one combined socket so the active runtime has exactly
        # one liquidation ingest path and one reconnect/epoch boundary.
        streams = f"{symbol.lower()}@aggTrade/{symbol.lower()}@forceOrder"
        url = f"wss://fstream.binance.com/market/stream?streams={streams}"
        _ensure_ring(state)
        while True:
            try:
                async with mod.websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    reset_futures_epoch(state)
                    reset_liquidation = (
                        force_order_epoch_reset
                        if callable(force_order_epoch_reset)
                        else liquidation_context.reset_epoch
                    )
                    reset_liquidation(state)
                    last_agg_id = None
                    logging.info("[FUTURES FLOW] hardened aggTrade+forceOrder collector epoch=%s: %s", state.futures_flow_epoch, symbol.upper())
                    async for raw in ws:
                        try:
                            data = _unwrap_combined_stream(mod.orjson.loads(raw))
                            recv_ms = time.time() * 1000.0
                            recv_mono_ns = time.monotonic_ns()
                            if str(data.get("e", "")) == "forceOrder":
                                _dispatch_futures_payload(
                                    state, data, recv_ms, recv_mono_ns,
                                    force_order_observer,
                                )
                                continue
                            agg_id = int(data.get("a", 0) or 0)
                            if (
                                last_agg_id is not None and agg_id > 0
                                and agg_id != last_agg_id + 1
                            ):
                                logging.warning(
                                    "[FUTURES FLOW] sequence gap expected=%s actual=%s; new epoch",
                                    last_agg_id + 1, agg_id,
                                )
                                reset_futures_epoch(state)
                            if _dispatch_futures_payload(
                                state, data, recv_ms, recv_mono_ns,
                                force_order_observer,
                            ) == "TRADE" and agg_id > 0:
                                last_agg_id = agg_id
                        except (KeyError, TypeError, ValueError):
                            continue
            except asyncio.CancelledError:
                raise
            except mod.websockets.exceptions.ConnectionClosed as exc:
                logging.warning("[FUTURES FLOW] reconnect: %s", exc)
                await asyncio.sleep(3)
            except Exception:
                logging.exception("[FUTURES FLOW] hardened collector failure")
                await asyncio.sleep(3)

    async def liquidations(symbol: str, state):
        """Dedicated forceOrder lane; never contaminates aggTrade flow."""
        stream = f"{symbol.lower()}@forceOrder"
        url = f"wss://fstream.binance.com/market/stream?streams={stream}"
        while True:
            try:
                async with mod.websockets.connect(
                    url, ping_interval=20, ping_timeout=20
                ) as ws:
                    liquidation_context.reset_epoch(state)
                    state.liquidation_stream_connected = True
                    state.liquidation_stream_connected_at = time.time()
                    logging.info(
                        "[FUTURES LIQUIDATION] context collector epoch=%s: %s",
                        state.liquidation_epoch, symbol.upper(),
                    )
                    async for raw in ws:
                        try:
                            data = _unwrap_combined_stream(mod.orjson.loads(raw))
                            liquidation_context.observe_force_order(
                                state, data, time.time() * 1000.0
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
            except asyncio.CancelledError:
                state.liquidation_stream_connected = False
                raise
            except mod.websockets.exceptions.ConnectionClosed as exc:
                state.liquidation_stream_connected = False
                logging.warning("[FUTURES LIQUIDATION] reconnect: %s", exc)
                await asyncio.sleep(3)
            except Exception:
                state.liquidation_stream_connected = False
                logging.exception("[FUTURES LIQUIDATION] collector failure")
                await asyncio.sleep(3)

    mod.hung_dong_tien_spot = spot_local_time
    # Historical alias: this name also pointed at the Spot collector.
    mod.hung_dong_tien_futures = spot_local_time
    mod.hung_dong_tien_futures_real = hardened
    mod.hung_force_order_futures = liquidations
    return hardened
