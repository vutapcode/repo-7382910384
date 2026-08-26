"""Application-level idle recovery for public market-data WebSocket collectors."""
import asyncio
import contextlib
import logging
import time

VERSION = "WS_IDLE_RECOVERY_V4_MONOTONIC_PROGRESS"
FLOW_IDLE_SECONDS = 10.0
CHECK_SECONDS = 1.0


def _clear_queue(state, name):
    value = getattr(state, name, None)
    if hasattr(value, "clear"):
        value.clear()


def _reset_spot_causal_epoch(state):
    """Drop only short-lived Spot evidence that must not bridge a feed gap."""
    _clear_queue(state, "danh_sach_khop_lenh")
    _clear_queue(state, "flow_1s_buffer")
    _clear_queue(state, "trade_flow_timeline")
    _clear_queue(state, "_micro_regime_hist")
    state.thoi_gian_dong_tien_cuoi = 0.0
    state.last_trade_event_time_s = 0.0
    state.last_3s_window_ts = 0.0
    state.current_vol_3s = 0.0
    state.current_cvd_sell_3s = 0.0
    state.current_cvd_buy_3s = 0.0


def _reset_spot(state):
    _reset_spot_causal_epoch(state)


def _reset_futures(state):
    _clear_queue(state, "danh_sach_khop_lenh_futures")
    _clear_queue(state, "futures_flow_1s_buffer")
    state.futures_flow_60s_coverage_sec = 0.0
    state.thoi_gian_dong_tien_futures_cuoi = 0.0


def _reset_coinbase(state):
    state.thoi_gian_coinbase_ticker_cuoi = 0.0
    state.thoi_gian_coinbase_cuoi = 0.0
    state.coinbase_flow_3s_ts = 0.0
    state.coinbase_cvd_3s = 0.0
    state.coinbase_volume_3s = 0.0
    state.coinbase_cvd_1m = 0.0
    state.coinbase_volume_1m = 0.0
    state.coinbase_flow_1m_coverage_sec = 0.0
    state.coinbase_cvd_5m = 0.0


def _progress_marker(state, names):
    marker = []
    for name in names:
        try:
            marker.append(float(getattr(state, name, 0.0) or 0.0))
        except (TypeError, ValueError):
            marker.append(0.0)
    return tuple(marker)


def _wrap(module, name, label, timestamp_names, reset):
    original = getattr(module, name)
    marker_attr = f"_tier_s_idle_wrapped_{name}"
    if getattr(module, marker_attr, False):
        return

    async def guarded(*args, **kwargs):
        state = args[-1] if args else kwargs.get("bo_nho_ram")
        while True:
            child = asyncio.create_task(original(*args, **kwargs))
            last_marker = _progress_marker(state, timestamp_names)
            last_progress_mono = time.monotonic()
            try:
                while True:
                    done, _ = await asyncio.wait({child}, timeout=CHECK_SECONDS)
                    if child in done:
                        return await child

                    current_marker = _progress_marker(state, timestamp_names)
                    if current_marker != last_marker:
                        last_marker = current_marker
                        last_progress_mono = time.monotonic()
                        continue

                    if time.monotonic() - last_progress_mono <= FLOW_IDLE_SECONDS:
                        continue

                    reset(state)
                    child.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await child
                    logging.warning(
                        "[WS-IDLE] %s no market-data progress for %.1fs; reset epoch + reconnect",
                        label,
                        FLOW_IDLE_SECONDS,
                    )
                    break
            finally:
                if not child.done():
                    child.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await child

    setattr(module, name, guarded)
    setattr(module, marker_attr, True)


def install(app):
    modules = app.m
    _wrap(
        modules.tai_dong_tien,
        "hung_dong_tien_spot",
        "binance_spot_aggTrade",
        ("thoi_gian_dong_tien_cuoi",),
        _reset_spot,
    )
    _wrap(
        modules.tai_dong_tien,
        "hung_dong_tien_futures_real",
        "binance_futures_aggTrade",
        ("thoi_gian_dong_tien_futures_cuoi",),
        _reset_futures,
    )
    _wrap(
        modules.tai_coinbase,
        "hung_coinbase_spot",
        "coinbase_spot",
        ("thoi_gian_coinbase_ticker_cuoi", "thoi_gian_coinbase_cuoi"),
        _reset_coinbase,
    )
    app.state.ws_idle_recovery_version = VERSION
    return VERSION
