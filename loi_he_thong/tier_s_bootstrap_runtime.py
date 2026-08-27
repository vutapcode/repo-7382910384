"""Runtime core for the lean Binance Futures MAINNET Tier-S bootstrap."""
import asyncio
import faulthandler
import json
import logging
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

from dotenv import load_dotenv
from recorder.metadata import code_version, strategy_config_version
from loi_he_thong.runtime_lock import DuplicateInstanceError, acquire_runtime_lock
from loi_he_thong import mainnet_safety, strategy_profile
from loi_he_thong import tier_s_bootstrap_modules as m

try:
    import uvloop
except ImportError:
    uvloop = None

load_dotenv()
CURRENT_DIR = m.CURRENT_DIR
BOT_HEARTBEAT_PATH = Path(os.getenv(
    "SMC_BOT_HEARTBEAT_PATH",
    "/home/ubuntu/smc2026_data/health/bot_runtime.json",
))
state = m.bo_nho_ram.state
load_module = m.load_module

requested_execution = os.getenv("SMC_ENABLE_TRADING", "false").lower() in ("1", "true", "yes", "on")
state.execution_allowed = bool(requested_execution and mainnet_safety.mainnet_armed())
state.runtime_project_root = str(CURRENT_DIR)
state.code_version = code_version(CURRENT_DIR)
state.strategy_config_version = strategy_config_version()
state.strategy_profile =strategy_profile.current_profile()
state.entry_economics_v3_replay_approved = os.getenv(
    "WSTRADE_ENTRY_ECONOMICS_V3_REPLAY_APPROVED", "false"
).strip().lower() in ("1", "true", "yes", "on")
state.execution_venue = "BINANCE_FUTURES_MAINNET"

_api_key = mainnet_safety.credential("binance_api_key", "BINANCE_API_KEY")
_api_secret = mainnet_safety.credential("binance_api_secret", "BINANCE_API_SECRET")
api = m.binance_api.BinanceAPI(api_key=_api_key, secret_key=_api_secret)
# Keep only a boolean capability marker outside the SDK client. This prevents
# credential-free AUTO_PROMOTE shadow runs from polling a private endpoint.
api.has_private_credentials = bool(_api_key and _api_secret)
del _api_key, _api_secret
# Compatibility marker for legacy safety helpers; BinanceAPI itself is mainnet-only.
api.testnet = False


async def supervise(name, factory):
    while True:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.system_ready = False
            state.trading_enabled = False
            logging.exception("[SUPERVISOR] %s failed: %s; restart in 2s", name, exc)
            await asyncio.sleep(2.0)


# Backward-compatible public name used by the stable kernel contract.
supervisor = supervise


def _write_bot_heartbeat(payload):
    BOT_HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="bot_runtime_", suffix=".tmp", dir=BOT_HEARTBEAT_PATH.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, BOT_HEARTBEAT_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def vong_lap_runtime_heartbeat():
    while True:
        now = time.time()
        payload = {
            "schema_version": 1,
            "updated_at_ms": int(now * 1000),
            "pid": os.getpid(),
            "run_id": getattr(state, "run_id", None),
            "code_version": getattr(state, "code_version", None),
            "strategy_config_version": getattr(state, "strategy_config_version", None),
            "strategy_profile": getattr(state, "strategy_profile", None),
            "scorer_version": os.getenv("SMC_SCORER_VERSION", "IGNITION_CORE_V1"),
            "entry_lifecycle": os.getenv("SMC_ENTRY_LIFECYCLE", "PREDICT_PROBE_PROVE"),
            "execution_venue": "BINANCE_FUTURES_MAINNET",
            "system_ready": bool(getattr(state, "system_ready", False)),
            "trading_enabled": bool(getattr(state, "trading_enabled", False)),
            "readiness_reason": getattr(state, "last_readiness_reason", None),
            "position_status": getattr(state, "position_status", None),
            "decision_revision": int(getattr(state, "decision_revision", 0) or 0),
            "host_cpu_15m_pct": float(getattr(state, "host_cpu_15m_pct", 0.0) or 0.0),
            "host_cpu_1h_pct": float(getattr(state, "host_cpu_1h_pct", 0.0) or 0.0),
            "host_cpu_p95_pct": float(
                getattr(state, "host_cpu_p95_pct", 0.0) or 0.0
            ),
            "cpu_budget_15m_remaining": float(
                getattr(state, "cpu_budget_15m_remaining", 0.0) or 0.0
            ),
            "cpu_budget_1h_remaining": float(
                getattr(state, "cpu_budget_1h_remaining", 0.0) or 0.0
            ),
            "governor_mode": getattr(state, "governor_mode", "WARMUP"),
            "live_entry_cpu_allowed": bool(
                getattr(state, "live_entry_cpu_allowed", False)
            ),
            "shadow_entry_cpu_allowed": bool(
                getattr(state, "shadow_entry_cpu_allowed", True)
            ),
            "shadow_cpu_scheduler_mode": getattr(
                state, "shadow_cpu_scheduler_mode", "WARMUP"
            ),
            "shadow_entry_eval_interval_seconds": float(
                getattr(state, "shadow_entry_eval_interval_seconds", 0.0) or 0.0
            ),
            "shadow_entry_idle_skips": int(
                getattr(state, "shadow_entry_idle_skips", 0) or 0
            ),
            "cpu_history_restored": bool(
                getattr(state, "cpu_history_restored", False)
            ),
            "cpu_history_window_start_ms": getattr(
                state, "cpu_history_window_start_ms", None
            ),
            "cpu_governor_started_at_ms": getattr(
                state, "cpu_governor_started_at_ms", None
            ),
            "cpu_post_start_coverage_15m_seconds": float(
                getattr(state, "cpu_post_start_coverage_15m_seconds", 0.0) or 0.0
            ),
            "cpu_post_start_coverage_1h_seconds": float(
                getattr(state, "cpu_post_start_coverage_1h_seconds", 0.0) or 0.0
            ),
            "top_cpu_processes": getattr(state, "host_cpu_top_processes", []),
            "production_blockers": getattr(state, "production_workload_blockers", []),
            "lightsail_cpu_last_seen": getattr(state, "lightsail_cpu_last_seen", None),
            "metric_age_seconds": getattr(state, "lightsail_metric_age_seconds", None),
            "wstrade_promotion_status": getattr(state, "wstrade_promotion_status", None),
            "wstrade_live_armed": bool(getattr(state, "wstrade_live_armed", False)),
            "wstrade_user_stream_ready": bool(
                getattr(state, "wstrade_user_stream_ready", False)
            ),
            "wstrade_user_stream_epoch": int(
                getattr(state, "wstrade_user_stream_epoch", 0) or 0
            ),
            "wstrade_user_stream_last_event_at": getattr(
                state, "wstrade_user_stream_last_event_at", None
            ),
            "futures_flow_ring_size": int(
                getattr(state, "futures_flow_ring_size", 0) or 0
            ),
            "oi_poll_interval_seconds": float(
                getattr(state, "oi_poll_interval_effective_seconds", 15.0) or 15.0
            ),
            "strategy_authority": "IGNITION_CORE_V1",
            "canonical_opportunities": int(
                getattr(state, "canonical_opportunity_count", 0) or 0
            ),
            "canonical_qualified": int(
                getattr(state, "canonical_opportunity_qualified", 0) or 0
            ),
            "canonical_captured": int(
                getattr(state, "canonical_opportunity_captured", 0) or 0
            ),
            "entry_decision": getattr(state, "entry_shadow_decision", None),
            "entry_reason": (
                getattr(state, "entry_shadow_council", {}) or {}
            ).get("reason"),
            "entry_edge_class": getattr(state, "entry_edge_class", None),
            "ignition": dict(
                (getattr(state, "entry_shadow_council", {}) or {}).get("ignition")
                or {}
            ),
            "mainnet_commission_verified": bool(
                getattr(state, "mainnet_commission_verified", False)
            ),
            "mainnet_commission_source": getattr(
                state, "mainnet_commission_source", None
            ),
            "mainnet_maker_fee_bps": getattr(state, "mainnet_maker_fee_bps", None),
            "mainnet_taker_fee_bps": getattr(state, "mainnet_taker_fee_bps", None),
            "entry_cost_model": (
                getattr(state, "entry_edge_tier", {}) or {}
            ).get("cost_components"),
            "entry_flow_quality": getattr(
                state, "entry_tier_s_volume_quality", None
            ),
            "entry_exchange_independence": (
                getattr(state, "entry_shadow_council", {}) or {}
            ).get("exchange_independence"),
            "guardian_latency_p95_ms": getattr(state, "guardian_latency_p95_ms", None),
            "guardian_latency_samples": int(
                getattr(state, "guardian_latency_samples", 0) or 0
            ),
            "guardian_latency_samples_total": int(
                getattr(state, "guardian_latency_samples_total", 0) or 0
            ),
        }
        await asyncio.to_thread(_write_bot_heartbeat, payload)
        await asyncio.sleep(5.0)


def parse_btc_filters(exchange_info):
    symbol = next(
        (item for item in exchange_info.get("symbols", []) if item.get("symbol") == "BTCUSDT"),
        None,
    )
    if symbol is None:
        return {}
    filters = {item["filterType"]: item for item in symbol.get("filters", [])}
    lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE", {})
    price = filters.get("PRICE_FILTER", {})
    notional = filters.get("MIN_NOTIONAL", {})
    return {
        "step_size": float(lot.get("stepSize", 0.001)),
        "min_qty": float(lot.get("minQty", 0.001)),
        "max_qty": float(lot.get("maxQty", 0.0)),
        "tick_size": float(price.get("tickSize", 0.1)),
        "min_notional": float(notional.get("notional", 0.0)),
    }


async def khoi_tao_tai_khoan():
    state.account_ready = False
    state.balance_usdt = await api.get_balance()
    hedge_mode = await api.get_position_mode()
    info, status = await api.get_exchange_info()
    if hedge_mode is not None:
        state.account_hedge_mode = hedge_mode
    if status == 200 and isinstance(info, dict):
        state.exchange_filters = parse_btc_filters(info)

    config_ready, config_reason = mainnet_safety.validate_static_config(state.exchange_filters)
    mainnet_ready, mainnet_reason = await mainnet_safety.prepare_mainnet_account(api, state)
    hedge_mode = await api.get_position_mode()
    if hedge_mode is not None:
        state.account_hedge_mode = hedge_mode

    required = ("step_size", "min_qty", "tick_size", "min_notional")
    filters_ready = all(
        float(state.exchange_filters.get(name, 0.0) or 0.0) > 0 for name in required
    )
    state.account_ready = bool(
        state.balance_usdt > 0
        and hedge_mode is not None
        and status == 200
        and filters_ready
        and config_ready
        and mainnet_ready
        and hedge_mode is True
    )
    if not state.account_ready:
        state.last_readiness_reason = config_reason if not config_ready else mainnet_reason
        logging.critical(
            "[ACCOUNT] MAINNET startup not ready (%s); entry fail-closed",
            state.last_readiness_reason,
        )


def seconds_to_next_boundary(interval_seconds, settle_seconds=1.0):
    now = time.time()
    return interval_seconds - (now % interval_seconds) + settle_seconds


async def main():
    from loi_he_thong import tier_s_runtime_prune
    return await tier_s_runtime_prune._lean_main(sys.modules["khoi_dong"])


def run_direct():
    try:
        runtime_lock = acquire_runtime_lock("bot")
    except DuplicateInstanceError as exc:
        logging.critical("[RUNTIME] %s", exc)
        raise SystemExit(73) from exc
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        if uvloop is not None:
            uvloop.install()
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Tier-S runtime stopped.")
    finally:
        runtime_lock.close()
