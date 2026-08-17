"""Mainnet shadow Futures-flow hardening: local-time windows + saturation fail-closed."""
import asyncio
from collections import deque
import logging
import time

RETENTION_MS = 20_000.0
HARD_MAX = 12_000
REQUIRED_COVERAGE_SEC = 15.0


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
    """Replace only the public Mainnet Futures aggTrade collector used by shadow runtime."""
    mod = base.app.tai_dong_tien

    async def hardened(symbol: str, state):
        url = f"wss://fstream.binance.com/ws/{symbol.lower()}@aggTrade"
        _ensure_ring(state)
        while True:
            try:
                async with mod.websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    logging.info("[FUTURES FLOW] hardened Mainnet local-time collector: %s", symbol.upper())
                    async for raw in ws:
                        try:
                            data = mod.orjson.loads(raw)
                            recv_ms = time.time() * 1000.0
                            row = {
                                "gia": float(data["p"]),
                                "khoi_luong": float(data["q"]),
                                "ban_chu_dong": bool(data["m"]),
                                "thoi_gian_ms": int(recv_ms),
                                "exchange_time_ms": int(data.get("E", 0) or 0),
                                "nguon": "FUTURES",
                            }
                            ring = _ensure_ring(state)
                            ring.append(row)
                            _trim(state, recv_ms)
                            state.thoi_gian_dong_tien_futures_cuoi = recv_ms / 1000.0
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

    mod.hung_dong_tien_futures_real = hardened
    return hardened
