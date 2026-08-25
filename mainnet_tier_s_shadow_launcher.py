"""Active Tier-S Ignition Core runtime with SHADOW/AUTO_PROMOTE execution modes.

Entry authority is exclusively frozen Bias -> Ignition -> residual Edge.
Retired Entry Council and research modules must not be wired here. The
canonical root is `mainnet_tier_s_lean_launcher.py`.
"""
import asyncio
from collections import deque
import faulthandler
import json
import logging
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import time

from loi_he_thong import canonical_opportunity
from loi_he_thong import execution_causal_revalidation
from loi_he_thong import host_cpu_governor
from loi_he_thong import microstructure_regime
from loi_he_thong import shadow_daily_loss
from loi_he_thong import shadow_execution_model
from loi_he_thong import verified_cost_model
from loi_he_thong.auto_promotion import PromotionController

os.environ["SMC_EXECUTION_VENUE"] = "MAINNET"
os.environ["SMC_ENABLE_TRADING"] = "false"
os.environ["SMC_MAINNET_ARMED"] = "false"
os.environ["SMC_MAINNET_EXCLUSIVE_ACCOUNT"] = "false"

import khoi_dong as app

VERSION = "MAINNET_TIER_S_SHADOW_V2_CPU_SPLIT"
ENTRY_POLL = 0.10
BIAS_SCOUT = 0.25
GUARD_POLL = 0.05
IDLE = 60.0
QTY_BTC = 0.001
LEVERAGE = 20
START_BALANCE_USDT = float(os.getenv("SMC_SHADOW_BALANCE_USDT", "5.4"))
FEE_BPS_PER_SIDE = float(os.getenv("SMC_SHADOW_FEE_BPS_PER_SIDE", "5.0"))
DAILY_LOSS_USDT = abs(float(os.getenv("WSTRADE_DAILY_LOSS_USDT", "0.60")))
# Shadow must collect every eligible test outcome.  It audits this threshold
# for comparison with live risk, but only real execution may enforce it.
SHADOW_DAILY_LOSS_ENFORCED = False
STATE_DIR = Path(os.getenv(
    "SMC_JOURNAL_DIR",
    "/home/ubuntu/.local/state/smc2026/mainnet_shadow",
))
EVENT_PATH = Path(os.getenv(
    "SMC_SHADOW_EVENTS_PATH",
    str(STATE_DIR / "events.jsonl"),
))

entry_council = app.load_module(
    "ignition_core_mainnet_shadow",
    app.CURRENT_DIR / "loi_he_thong" / "ignition_core.py",
)
bias_council = app.load_module(
    "bias_council_mainnet_shadow",
    app.CURRENT_DIR / "2_suy_luan_mapping" / "bias_council.py",
)
guardian_s = app.load_module(
    "guardian_s_mainnet_shadow",
    app.CURRENT_DIR / "3_thuc_thi" / "ve_si_lenh" / "guardian_s_tier.py",
)
GUARD_LATENCIES_MS = deque(maxlen=18_000)
_GUARD_LAST_MONO = 0.0
_GUARD_P95_AT = 0.0
_GUARD_SAMPLE_TOTAL = 0
live_execution = app.load_module(
    "wstrade_live_execution_runtime",
    app.CURRENT_DIR / "3_thuc_thi" / "wstrade_live_execution.py",
)
private_user_stream = app.load_module(
    "wstrade_private_user_stream_runtime",
    app.CURRENT_DIR / "loi_he_thong" / "private_user_stream.py",
)
RUNTIME_MODE = os.getenv("WSTRADE_MODE", "SHADOW").strip().upper()
AUTO_PROMOTE = RUNTIME_MODE == "AUTO_PROMOTE"
DIRECT_LIVE = RUNTIME_MODE == "DIRECT_LIVE"
LIVE_CAPABLE = AUTO_PROMOTE or DIRECT_LIVE
PROMOTION = PromotionController()
_POSITION_RECORD_AT = 0.0
_POSITION_RECORD_IDENTITY = None


def _live_entry_authority(state):
    """CPU may seal real entries, but must not censor shadow samples."""
    return bool(getattr(state, "wstrade_live_armed", False))


def _authority_delay(state, normal_delay):
    if _live_entry_authority(state):
        return host_cpu_governor.feature_delay(state, normal_delay)
    return float(normal_delay)


async def _idle(*_args, **_kwargs):
    while True:
        await asyncio.sleep(IDLE)


def _record_guardian_latency(active):
    global _GUARD_LAST_MONO, _GUARD_P95_AT, _GUARD_SAMPLE_TOTAL
    _GUARD_SAMPLE_TOTAL = max(
        _GUARD_SAMPLE_TOTAL,
        int(getattr(app.state, "guardian_latency_samples_total", 0) or 0),
    )
    mono = time.monotonic()
    if not active:
        _GUARD_LAST_MONO = 0.0
        return
    if _GUARD_LAST_MONO > 0.0:
        GUARD_LATENCIES_MS.append(max(0.0, (mono - _GUARD_LAST_MONO) * 1000.0))
        _GUARD_SAMPLE_TOTAL += 1
    _GUARD_LAST_MONO = mono
    app.state.guardian_latency_samples = len(GUARD_LATENCIES_MS)
    app.state.guardian_latency_samples_total = _GUARD_SAMPLE_TOTAL
    if GUARD_LATENCIES_MS and mono - _GUARD_P95_AT >= 5.0:
        ordered = sorted(GUARD_LATENCIES_MS)
        index = min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))
        app.state.guardian_latency_p95_ms = ordered[index]
        _GUARD_P95_AT = mono


def _disable(obj, name):
    if obj is not None and hasattr(obj, name):
        setattr(obj, name, _idle)


async def _blocked_mutation(*_args, **_kwargs):
    raise RuntimeError("MAINNET_SHADOW_BLOCKED_EXCHANGE_MUTATION")


def _block_exchange_mutations():
    api = app.api
    for name in (
        "new_order",
        "new_order_test",
        "new_algo_order",
        "cancel_order",
        "cancel_all_open_orders",
        "cancel_algo_order",
        "cancel_all_algo_orders",
        "change_position_mode",
        "change_multi_asset_mode",
        "change_margin_type",
        "change_leverage",
    ):
        if hasattr(api, name):
            setattr(api, name, _blocked_mutation)


async def _shadow_account_init():
    s = app.state
    s.balance_usdt = START_BALANCE_USDT
    s.account_ready = True
    s.execution_allowed = False
    s.execution_venue = "BINANCE_FUTURES_MAINNET_SHADOW"
    s._api_is_testnet = False
    s.mainnet_shadow = True
    s.mainnet_shadow_version = VERSION
    s.mainnet_shadow_qty_btc = QTY_BTC
    s.mainnet_shadow_leverage = LEVERAGE
    s.mainnet_shadow_balance_usdt = START_BALANCE_USDT
    s.mainnet_shadow_real_orders_blocked = True
    commission = await verified_cost_model.refresh_account_commission(
        app.api, s, fallback_per_side=FEE_BPS_PER_SIDE
    )
    logging.info(
        "[MAINNET-SHADOW] market data plus read-only account commission; "
        "real exchange mutations blocked; qty=%.3f BTC balance_model=%.2f USDT "
        "fee_source=%s maker=%.4fbps taker=%.4fbps",
        QTY_BTC,
        START_BALANCE_USDT,
        commission["source"],
        commission["maker_fee_bps"],
        commission["taker_fee_bps"],
    )


def _cheap_profile(klines):
    close = 0.0
    try:
        if klines:
            close = float(klines[-1][4])
    except (IndexError, TypeError, ValueError):
        pass
    if close <= 0:
        close = _spot_mid()
    return {"poc": close, "vah": close, "val": close, "lvn_zones": []}


def _spot_mid():
    s = app.state
    bid = float(getattr(s, "best_bid", 0.0) or 0.0)
    ask = float(getattr(s, "best_ask", 0.0) or 0.0)
    return (bid + ask) / 2.0 if bid > 0 and ask > bid else max(bid, ask)


def _execution_recovery_active(state):
    return bool(
        getattr(state, "execution_unknown", False)
        or getattr(state, "wstrade_execution_recovery_required", False)
    )


def _release_execution_reservation_if_safe(
    state, opportunity_id, reason, *, position=None, pending=None
):
    """Never make an uncertain live order eligible for a duplicate retry."""
    if position is not None or isinstance(pending, dict):
        return False
    if _execution_recovery_active(state):
        return False
    return canonical_opportunity.release(state, opportunity_id, reason=reason)


def _settle_reconciled_reservation(state, reconciliation):
    """Resolve a held reservation only from an authoritative REST outcome."""
    reserved_id = int(
        getattr(state, "canonical_reserved_opportunity_id", 0) or 0
    )
    if not reserved_id:
        return False
    reserved_context = dict(
        getattr(state, "canonical_reserved_context", {}) or {}
    )
    if reconciliation == "FLAT":
        return canonical_opportunity.release(
            state, reserved_id, reason="RECOVERY_VERIFIED_FLAT_NO_FILL"
        )
    if reconciliation in {
        "UNOWNED_POSITION_FLATTENED",
        "RECOVERY_VERIFIED_FLAT_AFTER_FILL",
    }:
        captured = canonical_opportunity.mark_captured(state, reserved_id)
        if captured:
            entry_council.capture_episode(
                state,
                reserved_context.get("causal_episode_id"),
                side=reserved_context.get("side"),
            )
        return captured
    return False


def _commit_live_execution_capture(state, result, position=None):
    """Commit a canonical episode for a position or verified fill→flatten."""
    result = dict(result or {})
    outcome = dict(
        getattr(state, "wstrade_live_last_entry_outcome", {}) or {}
    )
    opportunity_id = int(result.get("canonical_opportunity_id", 0) or 0)
    episode_id = str(result.get("causal_episode_id") or "")
    outcome_matches = bool(
        int(outcome.get("canonical_opportunity_id", 0) or 0) == opportunity_id
        and str(outcome.get("causal_episode_id") or "") == episode_id
    )
    captured_execution = bool(
        position is not None
        or (outcome_matches and outcome.get("capture_required"))
    )
    if not captured_execution:
        return False
    committed = canonical_opportunity.mark_captured(state, opportunity_id)
    if committed:
        entry_council.capture_episode(
            state,
            episode_id,
            side=result.get("side"),
            last_evidence_ms=(result.get("ignition") or {}).get(
                "last_evidence_ms"
            ),
        )
    return committed


def _latest_futures_price(now=None):
    now = time.time() if now is None else float(now)
    rows = getattr(app.state, "danh_sach_khop_lenh_futures", None) or ()
    try:
        row = rows[-1]
        ts = float(row.get("thoi_gian_ms", 0.0) or 0.0) / 1000.0
        px = float(row.get("gia", 0.0) or 0.0)
        if px > 0 and ts > 0 and now - ts <= 5.0:
            return px
    except (AttributeError, IndexError, TypeError, ValueError):
        pass
    return _spot_mid()


def _spot_fresh(now):
    ts = float(getattr(app.state, "thoi_gian_tick_cuoi", 0.0) or 0.0)
    return ts > 0 and now - ts <= 3.0

def _flow_volume_quorum(state, now):
    floor = max(
        0.02,
        min(0.10, 0.02 * float(getattr(state, "vol_pct90", 0.0) or 0.0)),
    )
    venues = {}

    spot_ts = float(getattr(state, "thoi_gian_dong_tien_cuoi", 0.0) or 0.0)
    spot_vol = (
        float(getattr(state, "current_cvd_buy_3s", 0.0) or 0.0)
        + float(getattr(state, "current_cvd_sell_3s", 0.0) or 0.0)
    )
    if spot_ts > 0 and now - spot_ts <= 5.0 and spot_vol >= floor:
        venues["spot"] = spot_vol

    cb_ts = float(getattr(state, "coinbase_flow_3s_ts", 0.0) or 0.0)
    cb_vol = float(getattr(state, "coinbase_volume_3s", 0.0) or 0.0)
    if cb_ts > 0 and now - cb_ts <= 5.0 and cb_vol >= floor:
        venues["coinbase"] = cb_vol

    cutoff = (now - 3.0) * 1000.0
    fut_vol = 0.0
    newest = 0.0
    for row in reversed(
        getattr(state, "danh_sach_khop_lenh_futures", ()) or ()
    ):
        try:
            ts_ms = float(row.get("thoi_gian_ms", 0.0) or 0.0)
            if ts_ms <= 0.0:
                continue
            if newest <= 0.0:
                newest = ts_ms / 1000.0
            if ts_ms < cutoff:
                break
            fut_vol += float(row.get("khoi_luong", 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
    if newest > 0 and now - newest <= 5.0 and fut_vol >= floor:
        venues["futures"] = fut_vol

    state.entry_tier_s_volume_quality = {
        "floor_btc": floor,
        "venues": venues,
        "required": 2,
    }
    return len(venues) >= 2


def _entry_quorum_ok(result, state, now):
    if not result or result.get("decision") != "GO":
        return False
    ignition = result.get("ignition") or {}
    return bool(
        ignition.get("state") == "PROVE"
        and ignition.get("proof_type") in (
            "METAORDER_CONTINUATION", "FAILED_REVERSION"
        )
        and ignition.get("cash_venues")
        and (
            ignition.get("proposer") != "futures"
            or ignition.get("futures_cash_response_ok")
        )
    )


def _entry_feasibility(price):
    notional = max(0.0, float(price)) * QTY_BTC
    margin = notional / max(LEVERAGE, 1)
    roundtrip_fee_reserve = notional * (2.0 * FEE_BPS_PER_SIDE) / 10000.0
    required = margin + roundtrip_fee_reserve
    return {
        "price": price,
        "qty_btc": QTY_BTC,
        "notional_usdt": notional,
        "leverage_model": LEVERAGE,
        "initial_margin_usdt": margin,
        "roundtrip_fee_reserve_usdt": roundtrip_fee_reserve,
        "required_usdt": required,
        "balance_usdt": float(
            getattr(app.state, "mainnet_shadow_balance_usdt", START_BALANCE_USDT)
            or 0.0
        ),
        "feasible": required <= float(
            getattr(app.state, "mainnet_shadow_balance_usdt", START_BALANCE_USDT)
            or 0.0
       ),
    }


def _append_event(event, payload):
    EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.time(),
        "runtime": VERSION,
        "event": event,
        **payload,
    }
    with EVENT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _decision_cycle_id(state, now):
    return "decision:%s:%d:%d" % (
        str(getattr(state, "run_id", "missing-run") or "missing-run")[:12],
        int(getattr(state, "decision_revision", 0) or 0),
        int(float(now) * 1000),
    )


def _execution_mid(state):
    bid = float(getattr(state, "execution_best_bid", 0.0) or 0.0)
    ask = float(getattr(state, "execution_best_ask", 0.0) or 0.0)
    if bid > 0.0 and ask >= bid:
        return (bid + ask) / 2.0
    try:
        return float(state.danh_sach_khop_lenh_futures[-1].get("gia", 0.0) or 0.0)
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0.0


def _miss_taxonomy(result, edge_report, quorum_ok):
    """Map active council/edge behavior to one stable primary miss reason."""
    reason = str((result or {}).get("reason", "") or "").upper()
    votes = (result or {}).get("s_votes") or {}
    s1 = votes.get("S1_cross_venue_price_acceptance") or {}
    s2 = votes.get("S2_multi_venue_executed_flow") or {}
    impact = (edge_report or {}).get("price_impact") or {}
    basis = (edge_report or {}).get("spot_perp_basis") or {}
    would_enter = (result or {}).get("decision") == "GO"
    failed = []
    if "STALE" in reason or "FEED_NOT_READY" in reason:
        failed.append("WAIT_STALE_DATA")
    if "EXTERNAL" in reason:
        failed.append("WAIT_EXTERNAL_CORROBORATION")
    if "CHASE" in reason:
        failed.append("WAIT_CHASE")
    if "ACCEPTANCE_PERSISTENCE" in reason:
        failed.append("WAIT_CAUSAL_PERSISTENCE")
    if "IGNITION_PROOF" in reason or "EVIDENCE_DECAYED" in reason:
        failed.append("WAIT_IGNITION_PROOF")
    if "FUTURES_ALERT_CASH_RESPONSE" in reason:
        failed.append("WAIT_CASH_RESPONSE")
    if "CAUSAL_LEADER_UNCERTAIN" in reason:
        failed.append("WAIT_LEADER_UNCERTAIN")
    if "OI_REFRESH" in reason:
        failed.append("WAIT_OI_REFRESH")
    if "OI_UNWIND" in reason or "OI_CLOSING" in reason:
        failed.append("WAIT_OI_CLOSING_CONTEXT")
    if "IMPULSE_ALREADY_CONSUMED" in reason:
        failed.append("WAIT_LATE_IMPULSE")
    if "OI_CLOSING" in reason:
        failed.append("WAIT_OI_CLOSING_CONTEXT")
    # Edge metadata is behavior only when the Council actually offered a GO
    # candidate to authorize. On a Council WAIT it is context, not a veto.
    if would_enter and bool(impact.get("absorbed")):
        failed.append("ABSORPTION_VETO")
    if would_enter and bool(basis.get("perp_expansion")):
        failed.append("PERP_LED_VETO")
    if "BIAS" in reason or str((result or {}).get("side", "")).upper() not in ("LONG", "SHORT"):
        failed.append("BIAS_NOT_READY")
    if s1 and str(s1.get("status", "MISSING")) != "PASS":
        failed.append("PRICE_QUORUM_FAIL")
    if s2 and str(s2.get("status", "MISSING")) != "PASS":
        failed.append("FLOW_QUORUM_FAIL")
    # Bootstrap shadow is intentionally allowed to trade so outcomes can make
    # the empirical gate measurable.  It is not a miss merely because the old
    # structural residual proxy is zero.  Only a final rejected GO is tagged.
    if (
        would_enter and not quorum_ok
        and not impact.get("absorbed") and not basis.get("perp_expansion")
    ):
        bootstrap = bool((edge_report or {}).get("bootstrap_shadow_allowed"))
        empirical = bool((edge_report or {}).get("live_empirical_ok"))
        if not bootstrap and not empirical:
            failed.append("EMPIRICAL_ALPHA_NOT_READY")
        elif not bool((edge_report or {}).get("cost_ok")) and not bootstrap:
            failed.append("EDGE_COST_FAIL")
    if would_enter and not quorum_ok and not failed:
        failed.append("FLOW_QUORUM_FAIL")
    priority = (
        "WAIT_STALE_DATA", "WAIT_EXTERNAL_CORROBORATION", "WAIT_CHASE",
        "WAIT_CASH_RESPONSE", "WAIT_LEADER_UNCERTAIN", "WAIT_LATE_IMPULSE",
        "WAIT_IGNITION_PROOF", "WAIT_CAUSAL_PERSISTENCE", "WAIT_OI_REFRESH",
        "WAIT_OI_CLOSING_CONTEXT",
        "ABSORPTION_VETO", "PERP_LED_VETO", "EMPIRICAL_ALPHA_NOT_READY",
        "EDGE_COST_FAIL",
        "BIAS_NOT_READY", "PRICE_QUORUM_FAIL", "FLOW_QUORUM_FAIL",
    )
    unique = list(dict.fromkeys(failed))
    primary = next((name for name in priority if name in unique), None)
    return primary, unique


def _decision_snapshot(state, result, edge_report, quorum_ok, cycle_id, now, opportunity=None):
    votes = (result or {}).get("s_votes") or {}
    bias = dict(getattr(state, "bias_council", {}) or {})
    cb_ts = float(
        getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0)
        or getattr(state, "thoi_gian_coinbase_cuoi", 0.0) or 0.0
    )
    oi_ts = float(getattr(state, "thoi_gian_vi_mo_cuoi", 0.0) or 0.0)
    reference = _execution_mid(state)
    side = str((result or {}).get("side", "ABSTAIN") or "ABSTAIN").upper()
    causal = dict((result or {}).get("causal") or {})
    miss, failed = _miss_taxonomy(result, edge_report, quorum_ok)
    hard_sl_bps = None
    if reference > 0.0 and side in ("LONG", "SHORT"):
        try:
            hard_sl, _ = live_execution._risk_geometry(state, side, reference)
            hard_sl_bps = abs(reference - float(hard_sl)) / reference * 10000.0
        except (AttributeError, TypeError, ValueError):
            hard_sl_bps = None
    return {
        "cycle_id": cycle_id,
        "decision_time_ms": int(now * 1000),
        "strategy_authority": "IGNITION_CORE_V1",
        "strategy_code_version": getattr(state, "code_version", None),
        "strategy_config_version": getattr(state, "strategy_config_version", None),
        "taxonomy_version": "TIER_S_MISS_TAXONOMY_V3_BOOTSTRAP_IS_NOT_MISS",
        "causal_episode_id": (opportunity or {}).get("causal_episode_id"),
        "inputs": {
            "bias": {
                "direction": getattr(state, "bias_state", "ABSTAIN"),
                "confidence": float(getattr(state, "bias_confidence", 0.0) or 0.0),
                "reason": bias.get("reason"),
                "hysteresis": bias.get("hysteresis"),
                "story": bias.get("story"),
                "s_votes": bias.get("s_votes"),
            },
            "s1_price_quorum": votes.get("S1_cross_venue_price_acceptance"),
            "s2_executed_flow_quorum": votes.get("S2_multi_venue_executed_flow"),
            "s3_causal_validator": votes.get("S3_causal_response_validator"),
            "open_interest": {
                "value": float(getattr(state, "open_interest", 0.0) or 0.0),
                "age_seconds": round(now - oi_ts, 4) if oi_ts > 0.0 else None,
                "fresh": bool(oi_ts > 0.0 and 0.0 <= now - oi_ts <= 15.0),
            },
            "regime": (edge_report or {}).get("micro_regime"),
            "spot_perp_relation": (edge_report or {}).get("spot_perp_basis"),
            "price_impact": (edge_report or {}).get("price_impact"),
            "coinbase": {
                "age_seconds": round(now - cb_ts, 4) if cb_ts > 0.0 else None,
                "freshness_timestamp": cb_ts or None,
            },
            "exchange_independence": (result or {}).get("exchange_independence"),
            "evidence_groups": causal.get("evidence_groups"),
            "flow_persistence": causal.get("persistence"),
            "oi_intent": causal.get("oi_intent"),
            "cash_perp_handoff": causal.get("handoff"),
            "post_chase_retest": causal.get("post_chase_retest"),
            "price_acceptance": causal.get("acceptance"),
            "ignition": dict((result or {}).get("ignition") or {}),
            "persistent_metaorder_shadow": dict(
                (result or {}).get("persistent_metaorder_shadow") or {}
            ),
        },
        "output": {
            "decision": (result or {}).get("decision", "WAIT"),
            "reason": (result or {}).get("reason", "UNKNOWN"),
            "side": side,
            "mode": (result or {}).get("entry_mode", "NONE"),
            "phase": (result or {}).get("phase"),
            "confidence": float((result or {}).get("confidence", 0.0) or 0.0),
            "quorum_ok": bool(quorum_ok),
            "edge_class": (edge_report or {}).get("edge_class"),
            "cost": {
                "cost_ok": (edge_report or {}).get("cost_ok"),
                "budget_bps": (edge_report or {}).get("cost_budget_bps_model"),
                "expected_bps_model": (edge_report or {}).get("expected_excursion_bps_model"),
                "expected_net_bps_model": (edge_report or {}).get("expected_net_bps_model"),
                "minimum_net_edge_bps": (edge_report or {}).get("min_net_edge_bps"),
                "multiple": (edge_report or {}).get("cost_multiple_model"),
                "execution_style": (edge_report or {}).get("execution_style"),
                "commission_verified": (edge_report or {}).get("commission_verified"),
                "commission_source": (edge_report or {}).get("commission_source"),
                "components": (edge_report or {}).get("cost_components"),
                "empirical_alpha": (edge_report or {}).get("empirical_alpha"),
            },
            "miss_taxonomy": miss,
            "failed_gates": failed,
        },
        "counterfactual": {
            "eligible": bool(miss and reference > 0.0 and side in ("LONG", "SHORT")),
            "reference_price": reference or None,
            "side": side,
            "hard_sl_bps": round(hard_sl_bps, 4) if hard_sl_bps is not None else None,
            "windows_seconds": [5, 15, 30, 60],
        },
    }


def _record_position_state(pos, guardian, risk, price, now, force=False):
    """Journal material Guardian/Risk changes without logging the 20 Hz loop."""
    global _POSITION_RECORD_AT, _POSITION_RECORD_IDENTITY
    guardian = dict(guardian or {})
    risk = dict(risk or {})
    identity = (
        str(guardian.get("decision", "HOLD")),
        str(guardian.get("reason", "UNKNOWN")),
        str(risk.get("decision", "HOLD")),
        str(risk.get("reason", "UNKNOWN")),
        round(float(getattr(pos, "best_r", 0.0) or 0.0), 1),
        round(float(getattr(pos, "floor_r", 0.0) or 0.0), 1),
        str(getattr(pos, "stage", "INITIAL")),
    )
    changed = identity != _POSITION_RECORD_IDENTITY
    if not force and not (
        (changed and now - _POSITION_RECORD_AT >= 1.0)
        or now - _POSITION_RECORD_AT >= 5.0
    ):
        return False
    try:
        regime = microstructure_regime.classify(app.state, pos.side)
    except Exception:
        regime = {"regime": "UNAVAILABLE"}
    _append_event("POSITION_STATE", {
        "schema_version": "TIER_S_SHADOW_EXECUTION_V1",
        "cycle_id": getattr(pos, "position_cycle_id", None),
        "decision_cycle_id": getattr(pos, "decision_cycle_id", None),
        "causal_episode_id": getattr(pos, "causal_episode_id", None),
        "side": getattr(pos, "side", None),
        "price": float(price or 0.0),
        "qty_btc": float(getattr(pos, "qty", 0.0) or 0.0),
        "holding_time_seconds": max(
            0.0, now - float(getattr(pos, "opened_at", now) or now)
        ),
        "hard_sl": getattr(pos, "hard_sl", None),
        "best_r": getattr(pos, "best_r", None),
        "floor_r": getattr(pos, "floor_r", None),
        "profit_floor": getattr(pos, "floor", None),
        "guardian_state": guardian,
        "risk_state": risk,
        "regime": regime,
    })
    _POSITION_RECORD_AT = now
    _POSITION_RECORD_IDENTITY = identity
    return True


def _open_shadow(side, result, now):
    if not bool(getattr(app.state, "mainnet_shadow_ready", False)):
        result = dict(result or {})
        reason = "SHADOW_EXECUTION_BBO_NOT_READY"
        app.state.mainnet_shadow_last_skip = reason
        bid = float(getattr(app.state, "execution_best_bid", 0.0) or 0.0)
        ask = float(getattr(app.state, "execution_best_ask", 0.0) or 0.0)
        reference = (
            ask if side == "LONG" else bid if side == "SHORT" else 0.0
        )
        _append_event("ENTRY_SKIPPED", {
            "schema_version": "TIER_S_SHADOW_EXECUTION_V1",
            "cycle_id": result.get("decision_cycle_id"),
            "causal_episode_id": result.get("causal_episode_id"),
            "reason": reason,
            "miss_taxonomy": "EXECUTION_NOT_CAPTURED",
            "failed_gates": [reason],
            "side": side,
            "entry": result,
            "counterfactual": {
                "eligible": bool(reference > 0.0),
                "reference_price": reference or None,
                "side": side,
                "hard_sl_bps": None,
                "windows_seconds": [5, 15, 30, 60],
            },
        })
        return None
    result = dict(result or {})
    execution = dict(result.pop("_shadow_execution", {}) or {})
    bid = float(getattr(app.state, "execution_best_bid", 0.0) or 0.0)
    ask = float(getattr(app.state, "execution_best_ask", 0.0) or 0.0)
    execution_policy = str(
        result.get("execution_policy", "MAKER") or "MAKER"
    ).upper()
    if not execution and execution_policy != "TAKER":
        limit_price = bid if side == "LONG" else ask
        if limit_price <= 0.0:
            app.state.mainnet_shadow_last_skip = "SHADOW_EXECUTION_BBO_MISSING"
            return None
        app.state.mainnet_shadow_pending_entry = {
            "side": side,
            "result": result,
            "placed_at": now,
            "expires_at": now + live_execution.MAKER_TTL_SECONDS,
            "limit_price": limit_price,
            "required_trade_through_volume": (
                shadow_execution_model.maker_fill_required_volume(QTY_BTC)
            ),
        }
        _append_event("SHADOW_MAKER_PLACED", {
            "cycle_id": result.get("decision_cycle_id"),
            "side": side,
            "limit_price": limit_price,
            "ttl_seconds": live_execution.MAKER_TTL_SECONDS,
            "execution_model": shadow_execution_model.VERSION,
        })
        return None
    if execution:
        price = float(execution.get("fill_price", 0.0) or 0.0)
    else:
        price = shadow_execution_model.market_fill(side, bid, ask)
    feasibility = _entry_feasibility(price)
    app.state.mainnet_shadow_last_feasibility = feasibility
    if price <= 0 or not feasibility["feasible"]:
        app.state.mainnet_shadow_last_skip = "SHADOW_BALANCE_INSUFFICIENT"
        _append_event(
            "ENTRY_SKIPPED",
            {
                "cycle_id": result.get("decision_cycle_id"),
                "causal_episode_id": result.get("causal_episode_id"),
                "reason": "SHADOW_BALANCE_INSUFFICIENT",
                "miss_taxonomy": "BALANCE/EXEC_FILTER",
                "side": side,
                "entry": result,
                "feasibility": feasibility,
                "counterfactual": {
                    "eligible": bool(price > 0.0),
                    "reference_price": price or None,
                    "side": side,
                    "hard_sl_bps": None,
                    "windows_seconds": [5, 15, 30, 60],
                },
            },
        )
        return None

    hard_sl, risk_plan = live_execution._risk_geometry(
        app.state, side, price
    )
    if not bool(risk_plan.get("eligible", False)):
        app.state.mainnet_shadow_last_skip = "SHADOW_RISK_GEOMETRY_REJECT"
        _append_event("ENTRY_SKIPPED", {
            "cycle_id": result.get("decision_cycle_id"),
            "causal_episode_id": result.get("causal_episode_id"),
            "reason": risk_plan.get("reason", "SHADOW_RISK_GEOMETRY_REJECT"),
            "miss_taxonomy": "RISK_GEOMETRY_FAIL",
            "side": side,
            "entry": result,
            "feasibility": feasibility,
            "risk_plan": risk_plan,
            "counterfactual": {
                "eligible": bool(price > 0.0),
                "reference_price": price or None,
                "side": side,
                "hard_sl_bps": (
                    abs(price - float(hard_sl)) / price * 10000.0
                    if price > 0.0 else None
                ),
                "windows_seconds": [5, 15, 30, 60],
            },
        })
        return None
    risk_distance = abs(float(price) - float(hard_sl))
    entry_regime = dict(
        ((result.get("edge_tier") or {}).get("micro_regime") or {})
    )
    shadow_cost_plan = verified_cost_model.shadow_execution_plan(
        result,
        app.state,
        (execution or {}).get("style", "MARKET"),
    )
    pos = SimpleNamespace(
        active=True,
        side=side,
        qty=QTY_BTC,
        initial_qty=QTY_BTC,
        opened_at=now,
        position_cycle_id=f"shadow:{side}:{int(now * 1000)}",
        entry_price=price,
        execution_entry_price=price,
        hard_sl=hard_sl,
        r=risk_distance,
        best_r=0.0,
        floor_r=None,
        floor=None,
        entry_regime=entry_regime,
        entry_edge_class=(result.get("edge_tier") or {}).get("edge_class"),
        entry_causal_thesis=live_execution._entry_causal_thesis(result),
        shadow_cost_plan=shadow_cost_plan,
        execution_cost_plan=shadow_cost_plan,
        decision_cycle_id=result.get("decision_cycle_id"),
        canonical_opportunity_id=int(
            result.get("canonical_opportunity_id", 0) or 0
        ),
        causal_episode_id=result.get("causal_episode_id"),
        shadow_execution=execution or {
            "style": "MARKET",
            "model": shadow_execution_model.VERSION,
        },
    )
    app.state.mainnet_shadow_position = pos
    canonical_opportunity.mark_captured(
        app.state, pos.canonical_opportunity_id
    )
    entry_council.capture_episode(
        app.state,
        pos.causal_episode_id,
        side=side,
        last_evidence_ms=(result.get("ignition") or {}).get("last_evidence_ms"),
    )
    app.state.mainnet_shadow_position_status = "OPEN"
    app.state.mainnet_shadow_last_entry = result
    _append_event(
        "ENTRY",
        {
            "schema_version": "TIER_S_SHADOW_EXECUTION_V1",
            "cycle_id": pos.position_cycle_id,
            "decision_cycle_id": result.get("decision_cycle_id"),
            "side": side,
            "price": price,
            "target_qty_btc": feasibility.get("target_qty_btc", QTY_BTC),
            "actual_qty_btc": float(pos.qty),
            "qty_btc": float(pos.qty),
            "entry_mode": result.get("entry_mode"),
            "phase": result.get("phase"),
            "confidence": result.get("confidence"),
            "edge_class": (result.get("edge_tier") or {}).get("edge_class"),
            "regime_at_entry": entry_regime,
            "entry_causal_thesis": pos.entry_causal_thesis,
            "canonical_opportunity_id": pos.canonical_opportunity_id,
            "causal_episode_id": pos.causal_episode_id,
            "feasibility": feasibility,
            "filter_status": (feasibility.get("execution_filters") or {}).get("mode"),
            "sizing_reason": feasibility.get("sizing_mode"),
            "fee_model": {
                "version": shadow_cost_plan["version"],
                "entry_fee_bps": shadow_cost_plan["entry_fee_bps"],
                "exit_fee_bps": shadow_cost_plan["exit_fee_bps"],
                "roundtrip_fee_bps": shadow_cost_plan["roundtrip_fee_bps"],
                "commission_verified": shadow_cost_plan["commission_verified"],
                "commission_source": shadow_cost_plan["commission_source"],
            },
            "slippage_model": {
                "version": shadow_execution_model.VERSION,
                "style": (pos.shadow_execution or {}).get("style"),
                "configured_market_bps": float(os.getenv(
                    "SMC_SHADOW_MARKET_SLIPPAGE_BPS", "1.5"
                )),
            },
            "hard_sl": hard_sl,
            "hard_sl_distance_bps": (
                risk_distance / price * 10000.0 if price > 0.0 else None
            ),
            "risk_plan": risk_plan,
            "best_r": 0.0,
            "floor_r": None,
            "shadow_execution": pos.shadow_execution,
        },
    )
    logging.info(
        "[MAINNET-SHADOW] ENTRY %s %.3f BTC @ %.2f mode=%s phase=%s",
        side,
        QTY_BTC,
        price,
        result.get("entry_mode"),
        result.get("phase"),
    )
    return pos


def _advance_shadow_pending(now):
    pending = getattr(app.state, "mainnet_shadow_pending_entry", None)
    if not isinstance(pending, dict):
        return None
    side = str(pending.get("side", "")).upper()
    limit_price = float(pending.get("limit_price", 0.0) or 0.0)
    volume = shadow_execution_model.maker_trade_through_volume(
        side,
        limit_price,
        float(pending.get("placed_at", 0.0) or 0.0),
        getattr(app.state, "danh_sach_khop_lenh_futures", ()),
        now=now,
    )
    required = float(pending.get("required_trade_through_volume", 0.0) or 0.0)
    if volume + 1e-12 >= required:
        execution = {
            "style": "MAKER_TRADE_THROUGH",
            "model": shadow_execution_model.VERSION,
            "fill_price": limit_price,
            "trade_through_volume": volume,
            "required_volume": required,
        }
    elif now < float(pending.get("expires_at", 0.0) or 0.0):
        return None
    else:
        result = dict(pending.get("result") or {})
        release, release_reason, release_detail = (
            execution_causal_revalidation.maker_ttl_release(
                app.state, side, result, now,
                float(pending.get("placed_at", 0.0) or 0.0),
            )
        )
        app.state.mainnet_shadow_maker_ttl_recheck = {
            "ok": release,
            "reason": release_reason,
            "detail": release_detail,
        }
        if not release:
            app.state.mainnet_shadow_pending_entry = None
            canonical_opportunity.release(
                app.state,
                int(result.get("canonical_opportunity_id", 0) or 0),
                reason=release_reason,
            )
            _append_event("SHADOW_MAKER_CANCELED", {
                "cycle_id": result.get("decision_cycle_id"),
                "causal_episode_id": result.get("causal_episode_id"),
                "reason": release_reason,
                "miss_taxonomy": "EXECUTION_NOT_FILLED",
                "failed_gates": [release_reason],
                "side": side,
                "limit_price": limit_price,
                "trade_through_volume": volume,
                "required_volume": required,
                "execution_model": shadow_execution_model.VERSION,
                "causal_recheck": release_detail,
                "counterfactual": {
                    "eligible": bool(limit_price > 0.0),
                    "reference_price": limit_price or None,
                    "side": side,
                    "hard_sl_bps": None,
                    "windows_seconds": [5, 15, 30, 60],
                },
            })
            _append_event("ENTRY_SKIPPED", {
                "schema_version": "TIER_S_SHADOW_EXECUTION_V1",
                "cycle_id": result.get("decision_cycle_id"),
                "causal_episode_id": result.get("causal_episode_id"),
                "reason": release_reason,
                "miss_taxonomy": "EXECUTION_NOT_CAPTURED",
                "failed_gates": [release_reason],
                "side": side,
                "entry": result,
                "counterfactual": {
                    "eligible": bool(limit_price > 0.0),
                    "reference_price": limit_price or None,
                    "side": side,
                    "hard_sl_bps": None,
                    "windows_seconds": [5, 15, 30, 60],
                },
            })
            return None
        bid = float(getattr(app.state, "execution_best_bid", 0.0) or 0.0)
        ask = float(getattr(app.state, "execution_best_ask", 0.0) or 0.0)
        fill = shadow_execution_model.market_fill(side, bid, ask)
        if fill <= 0.0:
            app.state.mainnet_shadow_pending_entry = None
            canonical_opportunity.release(
                app.state,
                int(result.get("canonical_opportunity_id", 0) or 0),
                reason="SHADOW_MARKET_FALLBACK_BBO_MISSING",
            )
            _append_event("ENTRY_SKIPPED", {
                "schema_version": "TIER_S_SHADOW_EXECUTION_V1",
                "cycle_id": result.get("decision_cycle_id"),
                "causal_episode_id": result.get("causal_episode_id"),
                "reason": "SHADOW_MARKET_FALLBACK_BBO_MISSING",
                "miss_taxonomy": "EXECUTION_NOT_CAPTURED",
                "failed_gates": ["SHADOW_MARKET_FALLBACK_BBO_MISSING"],
                "side": side,
                "entry": result,
                "counterfactual": {
                    "eligible": bool(limit_price > 0.0),
                    "reference_price": limit_price or None,
                    "side": side,
                    "hard_sl_bps": None,
                    "windows_seconds": [5, 15, 30, 60],
                },
            })
            return None
        execution = {
            "style": "MARKET_FALLBACK",
            "model": shadow_execution_model.VERSION,
            "fill_price": fill,
            "trade_through_volume": volume,
            "required_volume": required,
        }
    app.state.mainnet_shadow_pending_entry = None
    result = dict(pending.get("result") or {})
    result["_shadow_execution"] = execution
    position = _open_shadow(side, result, now)
    if position is None:
        canonical_opportunity.release(
            app.state,
            int(result.get("canonical_opportunity_id", 0) or 0),
            reason=str(
                getattr(
                    app.state, "mainnet_shadow_last_skip",
                    "MAKER_PENDING_TERMINAL_NO_FILL",
                ) or "MAKER_PENDING_TERMINAL_NO_FILL"
            ),
        )
    return position


def _close_shadow(pos, guardian_result, now):
    bid = float(getattr(app.state, "execution_best_bid", 0.0) or 0.0)
    ask = float(getattr(app.state, "execution_best_ask", 0.0) or 0.0)
    close_side = "SHORT" if pos.side == "LONG" else "LONG"
    price = shadow_execution_model.market_fill(close_side, bid, ask)
    if price <= 0:
        return
    entry = float(pos.entry_price)
    qty = float(getattr(pos, "qty", QTY_BTC) or QTY_BTC)
    gross = (
        (price - entry) * qty
        if pos.side == "LONG"
        else (entry - price) * qty
    )
    cost_plan = getattr(pos, "shadow_cost_plan", None) or {}
    entry_fee_bps = float(
        cost_plan.get("entry_fee_bps", FEE_BPS_PER_SIDE) or FEE_BPS_PER_SIDE
    )
    exit_fee_bps = float(
        cost_plan.get("exit_fee_bps", FEE_BPS_PER_SIDE) or FEE_BPS_PER_SIDE
    )
    fees = (
        entry * qty * entry_fee_bps / 10000.0
        + price * qty * exit_fee_bps / 10000.0
    )
    net = gross - fees
    stress_fees = (entry + price) * float(getattr(pos, "qty", QTY_BTC) or QTY_BTC) * 12.5 / 10000.0
    app.state.mainnet_shadow_stress_25bps_pnl = float(
        getattr(app.state, "mainnet_shadow_stress_25bps_pnl", 0.0) or 0.0
    ) + gross - stress_fees
    app.state.mainnet_shadow_balance_usdt = float(
        getattr(app.state, "mainnet_shadow_balance_usdt", START_BALANCE_USDT)
        or 0.0
    ) + net
    pos.active = False
    app.state.mainnet_shadow_position_status = "FLAT"
    app.state.mainnet_shadow_last_exit = guardian_result
    gross_bps = (
        (price - entry) / entry * 10000.0
        if pos.side == "LONG" else (entry - price) / entry * 10000.0
    )
    net_bps = net / max(entry * qty, 1e-12) * 10000.0
    net_r = net / max(float(getattr(pos, "r", 0.0) or 0.0) * qty, 1e-12)
    try:
        exit_regime = microstructure_regime.classify(app.state, pos.side)
    except Exception:
        exit_regime = {"regime": "UNAVAILABLE"}
    _append_event(
        "EXIT",
        {
            "schema_version": "TIER_S_SHADOW_EXECUTION_V1",
            "cycle_id": getattr(pos, "position_cycle_id", None),
            "decision_cycle_id": getattr(pos, "decision_cycle_id", None),
            "causal_episode_id": getattr(pos, "causal_episode_id", None),
            "side": pos.side,
            "entry_price": entry,
            "exit_price": price,
            "qty_btc": qty,
            "gross_pnl_usdt": gross,
            "fees_usdt": fees,
            "entry_fee_bps": entry_fee_bps,
            "exit_fee_bps": exit_fee_bps,
            "execution_cost_model": cost_plan or None,
            "net_pnl_usdt": net,
            "gross_pnl_bps": gross_bps,
            "net_pnl_bps": net_bps,
            "net_pnl_r": net_r,
            "holding_time_seconds": max(
                0.0, now - float(getattr(pos, "opened_at", now) or now)
            ),
            "hard_sl": getattr(pos, "hard_sl", None),
            "best_r": getattr(pos, "best_r", None),
            "floor_r": getattr(pos, "floor_r", None),
            "guardian_state": guardian_result,
            "risk_reason": (guardian_result or {}).get("reason"),
            "regime_at_entry": getattr(pos, "entry_regime", None),
            "regime_at_exit": exit_regime,
            "balance_usdt": app.state.mainnet_shadow_balance_usdt,
            "guardian": guardian_result,
        },
    )
    logging.info(
        "[MAINNET-SHADOW] EXIT %s %.3f BTC @ %.2f net=%+.4f balance=%.4f",
        pos.side,
        QTY_BTC,
        price,
        net,
        app.state.mainnet_shadow_balance_usdt,
    )


async def _open_position(side, result, now):
    if not bool(getattr(app.state, "mainnet_shadow_ready", False)):
        app.state.mainnet_shadow_last_skip = "STALE_ENTRY_RUNTIME_HEALTH"
        return None
    if bool(getattr(app.state, "wstrade_live_armed", False)):
        if not bool(getattr(app.state, "mainnet_live_entry_ready", False)) or not (
            host_cpu_governor.entry_allowed(app.state)
        ):
            app.state.mainnet_shadow_last_skip = "LIVE_HOST_CPU_OR_HEALTH_BLOCKED"
            return None
        edge_report = dict(getattr(app.state, "entry_edge_tier", {}) or {})
        if not bool(edge_report.get("live_empirical_ok", False)):
            app.state.mainnet_shadow_last_skip = "EMPIRICAL_ALPHA_NOT_READY"
            _append_event("ENTRY_SKIPPED", {
                "schema_version": "TIER_S_SHADOW_EXECUTION_V1",
                "cycle_id": result.get("decision_cycle_id"),
                "causal_episode_id": result.get("causal_episode_id"),
                "reason": "EMPIRICAL_ALPHA_NOT_READY",
                "miss_taxonomy": "EMPIRICAL_ALPHA_NOT_READY",
                "failed_gates": ["EMPIRICAL_ALPHA_NOT_READY"],
                "side": side,
                "entry": result,
                "empirical_alpha": edge_report.get("empirical_alpha"),
            })
            return None
        pos = await live_execution.open_position(
            app.api, app.state, side, result, now=now, event_callback=_append_event
        )
        _commit_live_execution_capture(app.state, result, position=pos)
        return pos
    return _open_shadow(side, result, now)


async def _close_position(pos, guardian_result, now):
    if bool(getattr(pos, "live", False)):
        return await live_execution.close_position(
            app.api, app.state, pos, guardian_result, now=now,
            event_callback=_append_event,
        )
    return _close_shadow(pos, guardian_result, now)


async def _promote_live(snapshot):
    s = app.state
    if not bool(getattr(s, "mainnet_live_entry_ready", False)) or not (
        host_cpu_governor.entry_allowed(s)
    ):
        s.wstrade_live_arm_reason = "LIVE_HOST_CPU_OR_HEALTH_BLOCKED"
        return False
    pos = getattr(s, "mainnet_shadow_position", None)
    if pos is not None and bool(getattr(pos, "active", False)):
        s.wstrade_live_arm_reason = "WAIT_SHADOW_POSITION_FLAT"
        return False
    if isinstance(getattr(s, "mainnet_shadow_pending_entry", None), dict):
        s.wstrade_live_arm_reason = "WAIT_SHADOW_MAKER_TERMINAL"
        return False
    promoted = await live_execution.promote(app.api, s)
    if promoted:
        s.mainnet_shadow = False
        s.mainnet_shadow_real_orders_blocked = False
        s.execution_venue = "BINANCE_FUTURES_MAINNET"
        event = "DIRECT_LIVE_ARMED" if DIRECT_LIVE else "AUTO_PROMOTED"
        _append_event(event, {"promotion": snapshot})
        if DIRECT_LIVE:
            logging.critical(
                "[WSTRADE] DIRECT_LIVE account preflight passed; Mainnet armed"
            )
        else:
            logging.critical("[WSTRADE] all promotion gates passed; Mainnet armed")
    else:
        logging.error(
            "[WSTRADE] promotion account preflight failed: %s",
            getattr(s, "wstrade_live_arm_reason", "UNKNOWN"),
        )
    return promoted


async def _demote_live(snapshot):
    """Seal new live entries while preserving exit/reconciliation authority."""
    s = app.state
    if bool(getattr(s, "wstrade_live_demote_pending", False)):
        return True
    s.wstrade_live_entry_allowed = False
    s.wstrade_live_demote_pending = True
    s.execution_allowed = False
    s.wstrade_live_arm_reason = "AUTO_DEMOTED:" + ",".join(
        snapshot.get("blockers") or ("UNKNOWN",)
    )
    pos = getattr(s, "mainnet_shadow_position", None)
    if pos is None or not bool(getattr(pos, "active", False)):
        s.wstrade_live_armed = False
        s.trading_enabled = False
        s.mainnet_shadow = True
        s.mainnet_shadow_real_orders_blocked = True
        s.execution_venue = "BINANCE_FUTURES_MAINNET_SHADOW"
        os.environ["SMC_ENABLE_TRADING"] = "false"
        os.environ["SMC_MAINNET_ARMED"] = "false"
        os.environ["SMC_MAINNET_EXCLUSIVE_ACCOUNT"] = "false"
    _append_event("AUTO_DEMOTED", {"promotion": snapshot})
    logging.critical("[WSTRADE] live entry authority sealed: %s", s.wstrade_live_arm_reason)
    return True


async def _promotion_loop():
    if DIRECT_LIVE:
        app.state.wstrade_promotion_status = "DIRECT_LIVE_WAITING"
        while True:
            try:
                s = app.state
                pos = getattr(s, "mainnet_shadow_position", None)
                if bool(getattr(s, "wstrade_live_armed", False)):
                    s.wstrade_promotion_status = "DIRECT_LIVE_ARMED"
                elif bool(getattr(s, "wstrade_execution_recovery_required", False)):
                    s.wstrade_promotion_status = "DIRECT_LIVE_RECOVERY"
                elif pos is not None and bool(getattr(pos, "active", False)):
                    s.wstrade_promotion_status = "DIRECT_LIVE_WAIT_POSITION_FLAT"
                elif not bool(getattr(s, "mainnet_shadow_ready", False)):
                    s.wstrade_promotion_status = "DIRECT_LIVE_WAIT_FEEDS"
                else:
                    snapshot = {
                        "mode": "DIRECT_LIVE",
                        "eligible": True,
                        "blockers": (),
                        "bypassed_gates": (
                            "REPLAY", "72H_SOAK", "OPPORTUNITY_RECALL",
                            "PROFIT_FACTOR", "EXPECTANCY", "STRESS_PNL",
                            "LIGHTSAIL_METRIC",
                        ),
                    }
                    promoted = await _promote_live(snapshot)
                    s.wstrade_promotion_status = (
                        "DIRECT_LIVE_ARMED" if promoted
                        else "DIRECT_LIVE_PREFLIGHT_BLOCKED"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                app.state.wstrade_promotion_status = "DIRECT_LIVE_ERROR"
                app.state.wstrade_promotion_error = f"{type(exc).__name__}: {exc}"
                logging.exception("[WSTRADE] DIRECT_LIVE arm failure")
            await asyncio.sleep(5.0 if not bool(
                getattr(app.state, "mainnet_shadow_ready", False)
            ) else 30.0)
    if not AUTO_PROMOTE:
        app.state.wstrade_promotion_status = "SHADOW_MODE"
        return await _idle()
    await PROMOTION.run(
        app.state,
        promote_callback=_promote_live,
        demote_callback=_demote_live,
        interval=60.0,
    )


async def _reconciliation_loop():
    while True:
        try:
            pos = getattr(app.state, "mainnet_shadow_position", None)
            live_position = bool(
                pos is not None
                and bool(getattr(pos, "active", False))
                and bool(getattr(pos, "live", False))
            )
            if (
                bool(getattr(app.state, "wstrade_live_armed", False))
                or live_position
                or bool(getattr(app.state, "wstrade_execution_recovery_required", False))
            ):
                reconciliation = await live_execution.reconcile(
                    app.api, app.state, event_callback=_append_event
                )
                _settle_reconciled_reservation(app.state, reconciliation)
        except asyncio.CancelledError:
            raise
        except Exception:
            app.state.wstrade_reconciliation_status = "ERROR"
            logging.exception("[WSTRADE] live reconciliation failure")
        await asyncio.sleep(2.0)


async def _private_user_stream_loop():
    if not LIVE_CAPABLE or not bool(
        getattr(app.api, "has_private_credentials", False)
    ):
        app.state.wstrade_user_stream_ready = False
        app.state.wstrade_user_stream_reason = "CREDENTIALS_NOT_PROVISIONED"
        return await _idle()
    await private_user_stream.run(
        app.api, app.state, event_callback=_append_event
    )


def _apply_runtime():
    if bool(getattr(app.api, "testnet", True)):
        raise RuntimeError("MAINNET_SHADOW_REQUIRES_MAINNET_PUBLIC_ENDPOINTS")

    if not LIVE_CAPABLE:
        _block_exchange_mutations()
    app.khoi_tao_tai_khoan = _shadow_account_init

    # Disable legacy execution authority; WStrade owns the only live executor.
    _disable(app.dat_lenh, "vong_lap_thuc_thi")
    _disable(app.bao_ve_khan_cap, "vong_lap_bao_ve")
    _disable(app.tho_san_trailing, "vong_lap_trailing")
    _disable(app.dong_bo_trang_thai, "vong_lap_dong_bo")
    _disable(app.dong_bo_trang_thai, "vong_lap_doi_chieu")
    _disable(app.nhat_ky_giao_dich, "vong_lap_nhat_ky")

    # Keep M1 ATR context; remove every physical legacy data/decision authority.
    _disable(getattr(app, "tai_so_lenh", None), "hung_so_lenh_futures")
    _disable(getattr(app, "tai_so_lenh", None), "hung_so_lenh_futures_execution")
    _disable(app, "vong_lap_nen_m15")
    _disable(app, "vong_lap_vi_mo_mapping")
    _disable(getattr(app, "map_gia_tick", None), "vong_lap_radar")

    app.footprint.cap_nhat_footprint = lambda *_a, **_k: None
    app.flash_flow.cap_nhat_nguong_ca_map = lambda *_a, **_k: None
    app.tri_oracle.cap_nhat_tri_oracle = lambda *_a, **_k: None
    app.POC_VAH_VAL.select_profile_klines = (
        lambda rows, *_a, **_k: list(rows[-2:]) if rows else []
    )
    app.POC_VAH_VAL.calculate_volume_profile = _cheap_profile

    s = app.state
    s.mainnet_shadow = True
    s.mainnet_shadow_version = VERSION
    s.mainnet_shadow_real_orders_blocked = True
    s.mainnet_shadow_qty_btc = QTY_BTC
    s.mainnet_shadow_balance_usdt = START_BALANCE_USDT

    old = getattr(s, "bias_price_history", None)
    if old is None or int(getattr(old, "maxlen", 0) or 0) < 128:
        s.bias_price_history = deque(list(old or ()), maxlen=256)

    fut_ring = getattr(s, "danh_sach_khop_lenh_futures", None)
    if fut_ring is None or int(getattr(fut_ring, "maxlen", 0) or 0) < 5000:
        s.danh_sach_khop_lenh_futures = deque(list(fut_ring or ()), maxlen=5000)

    logging.info(
        "[MAINNET-SHADOW] Tier-S causal runtime active; mode=%s mutations=%s",
        RUNTIME_MODE,
        "direct-live" if DIRECT_LIVE else "promotion-gated" if AUTO_PROMOTE else "blocked",
    )


async def _bias_loop():
    while True:
        try:
            s = app.state
            pos = getattr(s, "mainnet_shadow_position", None)
            if _live_entry_authority(s) and str(
                getattr(s, "governor_mode", "")
            ) == "SAFETY_ONLY" and not (
                pos is not None and bool(getattr(pos, "active", False))
            ):
                await asyncio.sleep(1.0)
                continue
            now = time.time()
            result = bias_council.evaluate(s, now=now)
            s.bias_state = result["bias"]
            s.bias_confidence = result["confidence"]
            s.bias_council = result
            s.bias_updated_at = now
            s.bias_version = result.get("version")
            s.macro_bias = "NEUTRAL"
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] fast bias scout failure")
        await asyncio.sleep(_authority_delay(app.state, BIAS_SCOUT))


async def _entry_loop():
    last_revision = None
    last_eval_at = 0.0
    last_decision_identity = None
    last_decision_event_at = 0.0
    while True:
        try:
            s = app.state
            now = time.time()
            pos = getattr(s, "mainnet_shadow_position", None)
            if pos is not None and bool(getattr(pos, "active", False)):
                # Position Guardian gets the urgent OI cadence; the collector
                # remains data-only and reads only this generic runtime hint.
                if float(
                    getattr(s, "oi_poll_interval_seconds", 15.0) or 15.0
                ) > 5.0:
                    app.tai_vi_mo.request_oi_refresh(s, "POSITION_GUARDIAN")
                s.oi_poll_interval_seconds = 5.0
                if not bool(getattr(pos, "live", False)):
                    daily = shadow_daily_loss.report(
                        s, pos,
                        bid=float(getattr(s, "execution_best_bid", 0.0) or 0.0),
                        ask=float(getattr(s, "execution_best_ask", 0.0) or 0.0),
                        fee_bps_per_side=FEE_BPS_PER_SIDE,
                        limit=DAILY_LOSS_USDT, now=now,
                        enforce=SHADOW_DAILY_LOSS_ENFORCED,
                    )
                    s.mainnet_shadow_daily_loss = daily
                    if daily["locked"]:
                        await _close_position(pos, {
                            "decision": "EXIT",
                            "reason": "DAILY_EQUITY_LOSS_BREAKER",
                            "daily_loss": daily,
                        }, now)
                        await asyncio.sleep(ENTRY_POLL)
                        continue
                await asyncio.sleep(ENTRY_POLL)
                continue
            daily_locked = False
            if not bool(getattr(s, "wstrade_live_armed", False)):
                daily = shadow_daily_loss.report(
                    s, limit=DAILY_LOSS_USDT, now=now,
                    enforce=SHADOW_DAILY_LOSS_ENFORCED,
                )
                s.mainnet_shadow_daily_loss = daily
                daily_locked = bool(daily["locked"])
                if daily_locked:
                    if isinstance(
                        getattr(s, "mainnet_shadow_pending_entry", None), dict
                    ):
                        pending = dict(s.mainnet_shadow_pending_entry)
                        pending_result = dict(pending.get("result") or {})
                        pending_side = str(pending.get("side", "")).upper()
                        pending_price = float(
                            pending.get("limit_price", 0.0) or 0.0
                        )
                        s.mainnet_shadow_pending_entry = None
                        _append_event("SHADOW_MAKER_CANCELED", {
                            "cycle_id": pending_result.get("decision_cycle_id"),
                            "causal_episode_id": pending_result.get("causal_episode_id"),
                            "reason": "DAILY_LOSS_LOCKED",
                            "miss_taxonomy": "RISK_DAILY_LOCK",
                            "side": pending_side,
                            "limit_price": pending_price,
                            "daily_loss": daily,
                            "counterfactual": {
                                "eligible": bool(
                                    pending_price > 0.0
                                    and pending_side in ("LONG", "SHORT")
                                ),
                                "reference_price": pending_price or None,
                                "side": pending_side,
                                "hard_sl_bps": None,
                                "windows_seconds": [5, 15, 30, 60],
                            },
                        })
                    s.mainnet_shadow_entry_state = "WAIT_DAILY_LOSS_LOCKED"
                    s.mainnet_shadow_last_skip = "DAILY_LOSS_LOCKED"
            if not daily_locked:
                _advance_shadow_pending(now)
            if isinstance(getattr(s, "mainnet_shadow_pending_entry", None), dict):
                await asyncio.sleep(ENTRY_POLL)
                continue
            if _live_entry_authority(s) and not host_cpu_governor.entry_allowed(s):
                s.mainnet_shadow_entry_state = "WAIT_HOST_CPU_BUDGET"
                await asyncio.sleep(host_cpu_governor.feature_delay(s, ENTRY_POLL))
                continue
            s.mainnet_shadow_cpu_degraded = not host_cpu_governor.entry_allowed(s)
            if not _spot_fresh(now):
                s.mainnet_shadow_entry_state = "WAIT_STALE_SPOT"
                await asyncio.sleep(ENTRY_POLL)
                continue

            revision = (
                int(getattr(s, "decision_revision", 0) or 0),
                round(float(getattr(s, "best_bid", 0.0) or 0.0), 2),
                round(float(getattr(s, "best_ask", 0.0) or 0.0), 2),
                round(float(getattr(s, "execution_best_bid", 0.0) or 0.0), 2),
                round(float(getattr(s, "execution_best_ask", 0.0) or 0.0), 2),
                round(float(getattr(s, "thoi_gian_dong_tien_cuoi", 0.0) or 0.0), 3),
                round(float(getattr(s, "coinbase_flow_3s_ts", 0.0) or 0.0), 3),
                round(float(getattr(s, "thoi_gian_dong_tien_futures_cuoi", 0.0) or 0.0), 3),
                round(float(getattr(s, "open_interest", 0.0) or 0.0), 3),
            )
            if revision == last_revision and now - last_eval_at < 0.50:
                await asyncio.sleep(_authority_delay(s, ENTRY_POLL))
                continue
            last_revision, last_eval_at = revision, now
            result = entry_council.evaluate(s, now=now)
            persistent_shadow = dict(
                getattr(s, "persistent_metaorder_shadow", {}) or {}
            )
            result = dict(result)
            result["persistent_metaorder_shadow"] = persistent_shadow
            urgent_oi_phase = str(result.get("phase", "")).upper() in {
                "PRESSURE_BUILDING", "ACCEPTANCE", "RELEASE",
            }
            previous_oi_interval = float(
                getattr(s, "oi_poll_interval_seconds", 15.0) or 15.0
            )
            s.oi_poll_interval_seconds = 5.0 if urgent_oi_phase else 15.0
            if urgent_oi_phase and previous_oi_interval > 5.0:
                app.tai_vi_mo.request_oi_refresh(s, "ENTRY_CAUSAL_PHASE")
            s.entry_shadow_council = result
            s.entry_shadow_decision = result["decision"]
            s.entry_shadow_confidence = result["confidence"]
            s.entry_shadow_phase = result.get("phase")
            s.entry_shadow_mode = result.get("entry_mode")
            s.entry_shadow_updated_at = now

            quorum_ok = _entry_quorum_ok(result, s, now)
            edge_report = dict(getattr(s, "entry_edge_tier", {}) or {})
            # Freeze the exact decision-time economics with the decision. This
            # prevents downstream execution from reading a newer, unrelated
            # state report or comparing maker and taker as if they were equal.
            result = dict(result)
            result["edge_tier"] = edge_report
            result["execution_cost_contract"] = dict(
                edge_report.get("execution_cost_contract") or {}
            )
            s.entry_shadow_council = result
            decision_cycle_id = _decision_cycle_id(s, now)
            result = dict(result)
            result["decision_cycle_id"] = decision_cycle_id
            s.entry_shadow_council = result
            vote_status = {
                name: str((payload or {}).get("status", "MISSING"))
                for name, payload in (result.get("s_votes") or {}).items()
            }
            if result.get("decision") != "GO":
                blocking_stage = "COUNCIL"
            elif not quorum_ok:
                blocking_stage = "EDGE_OR_QUORUM"
            else:
                blocking_stage = "READY"
            opportunity = canonical_opportunity.observe(
                s, result, qualified=quorum_ok, now=now
            )
            near_miss = bool(result.get("decision") == "GO" and not quorum_ok)
            s.mainnet_shadow_decision_evaluations = int(
                getattr(s, "mainnet_shadow_decision_evaluations", 0) or 0
            ) + 1
            if near_miss:
                s.mainnet_shadow_near_misses = int(
                    getattr(s, "mainnet_shadow_near_misses", 0) or 0
                ) + 1
            funnel = dict(getattr(s, "mainnet_shadow_funnel_counts", {}) or {})
            funnel[blocking_stage] = int(funnel.get(blocking_stage, 0) or 0) + 1
            s.mainnet_shadow_funnel_counts = funnel
            decision_identity = (
                str(result.get("decision", "WAIT")),
                str(result.get("reason", "UNKNOWN")),
                str(result.get("side", "ABSTAIN")),
                str(result.get("entry_mode", "NONE")),
                str(result.get("phase", "ARMED")),
                bool(quorum_ok),
                str(edge_report.get("edge_class", "UNCLASSIFIED")),
                blocking_stage,
                str(persistent_shadow.get("status", "OBSERVING")),
                str(persistent_shadow.get("candidate_side", "ABSTAIN")),
            )
            decision_changed = decision_identity != last_decision_identity
            # A qualified GO can exist for less than the telemetry debounce.
            # Persist its first transition so recorder/replay never sees only
            # the surrounding WAIT cycles.
            force_transition = bool(
                opportunity.get("new")
                or opportunity.get("qualification_transition")
                or persistent_shadow.get("transition")
            )
            decision_event_emitted = False
            recorder_snapshot = None
            if (
                force_transition
                or (decision_changed and now - last_decision_event_at >= 1.0)
                or now - last_decision_event_at >= 15.0
            ):
                last_decision_identity = decision_identity
                last_decision_event_at = now
                votes = result.get("s_votes") or {}
                price_quality = dict(
                    (votes.get("S1_cross_venue_price_acceptance") or {}).get(
                        "metrics"
                    ) or {}
                )
                flow_quality = dict(
                    (votes.get("S2_multi_venue_executed_flow") or {}).get(
                        "metrics"
                    ) or {}
                )
                recorder_snapshot = _decision_snapshot(
                    s, result, edge_report, quorum_ok, decision_cycle_id, now,
                    opportunity=opportunity,
                )
                _append_event("DECISION_EVALUATED", {
                    "schema_version": "TIER_S_DECISION_RECORD_V2",
                    "cycle_id": decision_cycle_id,
                    "decision": result.get("decision", "WAIT"),
                    "reason": result.get("reason", "UNKNOWN"),
                    "side": result.get("side", "ABSTAIN"),
                    "phase": result.get("phase"),
                    "entry_mode": result.get("entry_mode", "NONE"),
                    "quorum_ok": bool(quorum_ok),
                    "near_miss": near_miss,
                    "blocking_stage": blocking_stage,
                    "vote_status": vote_status,
                    "edge_class": edge_report.get("edge_class"),
                    "cost_ok": edge_report.get("cost_ok"),
                    "canonical_opportunity": opportunity,
                    "causal_episode_id": opportunity.get("causal_episode_id"),
                    "qualified_now": bool(opportunity.get("qualified_now")),
                    "qualified_ever": bool(opportunity.get("qualified_ever")),
                    "qualification_transition": bool(
                        opportunity.get("qualification_transition")
                    ),
                    "evidence_groups": (result.get("causal") or {}).get("evidence_groups"),
                    "flow_persistence": (result.get("causal") or {}).get("persistence"),
                    "oi_intent": (result.get("causal") or {}).get("oi_intent"),
                    "cash_perp_handoff": (result.get("causal") or {}).get("handoff"),
                    "post_chase_retest": (result.get("causal") or {}).get(
                        "post_chase_retest"
                    ),
                    "ignition": dict(result.get("ignition") or {}),
                    "persistent_metaorder_shadow": persistent_shadow,
                    "ignition_state": (result.get("ignition") or {}).get("state"),
                    "ignition_proposer": (result.get("ignition") or {}).get("proposer"),
                    "ignition_leader": (result.get("ignition") or {}).get("leader"),
                    "ignition_proof_type": (result.get("ignition") or {}).get("proof_type"),
                    "impulse_consumed_fraction": (result.get("ignition") or {}).get(
                        "consumed_fraction"
                    ),
                    "residual_edge_proxy_bps": (result.get("ignition") or {}).get(
                        "residual_edge_proxy_bps"
                    ),
                    "price_quality": price_quality,
                    "flow_quality": flow_quality,
                    "exchange_independence": result.get(
                        "exchange_independence"
                    ),
                    "price_impact": edge_report.get("price_impact"),
                    "spot_perp_basis": edge_report.get("spot_perp_basis"),
                    "governor_mode": getattr(s, "governor_mode", None),
                    "miss_taxonomy": recorder_snapshot["output"]["miss_taxonomy"],
                    "failed_gates": recorder_snapshot["output"]["failed_gates"],
                    "decision_record": recorder_snapshot,
                })
                decision_event_emitted = True

            if not quorum_ok:
                await asyncio.sleep(ENTRY_POLL)
                continue

            side = str(
                result.get("side") or getattr(s, "bias_state", "ABSTAIN")
            ).upper()
            if side not in ("LONG", "SHORT"):
                await asyncio.sleep(ENTRY_POLL)
                continue
            bias_side = str(getattr(s, "bias_state", "ABSTAIN")).upper()
            if bias_side in ("LONG", "SHORT") and side != bias_side:
                await asyncio.sleep(ENTRY_POLL)
                continue

            if daily_locked:
                s.mainnet_shadow_entry_state = "WAIT_DAILY_LOSS_LOCKED"
                s.mainnet_shadow_last_skip = "DAILY_LOSS_LOCKED"
                # Pair every persisted, otherwise executable GO decision with
                # an explicit execution outcome. This keeps the funnel honest:
                # a risk lock is not an unexplained strategy miss.
                if decision_event_emitted and recorder_snapshot is not None:
                    risk_lock_counterfactual = dict(
                        recorder_snapshot["counterfactual"]
                    )
                    risk_lock_counterfactual["eligible"] = bool(
                        risk_lock_counterfactual.get("reference_price")
                        and side in ("LONG", "SHORT")
                    )
                    _append_event("ENTRY_SKIPPED", {
                        "schema_version": "TIER_S_SHADOW_EXECUTION_V1",
                        "cycle_id": decision_cycle_id,
                        "causal_episode_id": opportunity.get("causal_episode_id"),
                        "taxonomy_version": recorder_snapshot["taxonomy_version"],
                        "strategy_code_version": recorder_snapshot[
                            "strategy_code_version"
                        ],
                        "strategy_config_version": recorder_snapshot[
                            "strategy_config_version"
                        ],
                        "reason": "DAILY_LOSS_LOCKED",
                        "miss_taxonomy": "RISK_DAILY_LOCK",
                        "failed_gates": ["RISK_DAILY_LOCK"],
                        "side": side,
                        "entry": result,
                        "daily_loss": daily,
                        "counterfactual": risk_lock_counterfactual,
                    })
                await asyncio.sleep(ENTRY_POLL)
                continue
            opportunity_id = int(opportunity.get("opportunity_id", 0) or 0)
            if not canonical_opportunity.claim(s, opportunity_id):
                reserve_reason = str(
                    getattr(
                        s, "canonical_last_reserve_reject",
                        "CANONICAL_RESERVATION_REJECTED",
                    ) or "CANONICAL_RESERVATION_REJECTED"
                )
                s.mainnet_shadow_entry_state = "WAIT_" + reserve_reason
                s.mainnet_shadow_last_skip = reserve_reason
                await asyncio.sleep(ENTRY_POLL)
                continue
            result = dict(result)
            result["canonical_opportunity_id"] = opportunity_id
            result["causal_episode_id"] = opportunity.get("causal_episode_id")
            result["execution_policy"] = (
                "TAKER"
                if str(result.get("phase", "")).upper() == "RELEASE"
                else "MAKER"
            )

            s.mainnet_shadow_entry_claim = (
                "CANONICAL_OPPORTUNITY", opportunity_id
            )
            s.mainnet_shadow_entry_claim_at = now
            try:
                position = await _open_position(side, result, now)
            except asyncio.CancelledError:
                _release_execution_reservation_if_safe(
                    s, opportunity_id, "EXECUTION_TASK_CANCELLED"
                )
                raise
            except Exception:
                _release_execution_reservation_if_safe(
                    s, opportunity_id, "EXECUTION_EXCEPTION"
                )
                raise
            pending = getattr(s, "mainnet_shadow_pending_entry", None)
            _release_execution_reservation_if_safe(
                s,
                opportunity_id,
                str(
                    getattr(
                        s, "mainnet_shadow_last_skip",
                        "EXECUTION_NOT_CAPTURED",
                    ) or "EXECUTION_NOT_CAPTURED"
                ),
                position=position,
                pending=pending,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] entry loop failure")
            await asyncio.sleep(0.5)
        await asyncio.sleep(_authority_delay(app.state, ENTRY_POLL))


async def _guardian_loop():
    while True:
        try:
            s = app.state
            pos = getattr(s, "mainnet_shadow_position", None)
            if pos is None or not bool(getattr(pos, "active", False)):
                _record_guardian_latency(False)
                await asyncio.sleep(0.10)
                continue
            _record_guardian_latency(True)
            now = time.time()
            if not _spot_fresh(now):
                s.guardian_s_decision = "HOLD_STALE_SPOT"
                await asyncio.sleep(GUARD_POLL)
                continue
            result = guardian_s.update_state(s, pos, now=now)
            if result.get("decision") == "EXIT":
                await _close_position(pos, result, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] guardian failure")
            await asyncio.sleep(0.25)
        await asyncio.sleep(GUARD_POLL)


async def _runtime():
    _apply_runtime()
    await asyncio.gather(
        app.main(),
        _bias_loop(),
        _entry_loop(),
        _guardian_loop(),
        _promotion_loop(),
        _reconciliation_loop(),
        _private_user_stream_loop(),
    )


def main():
    try:
        lock = app.acquire_runtime_lock("bot_mainnet_shadow")
    except app.DuplicateInstanceError as exc:
        logging.critical("[RUNTIME] %s", exc)
        raise SystemExit(73) from exc
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        if app.uvloop is not None:
            app.uvloop.install()
        asyncio.run(_runtime())
    except KeyboardInterrupt:
        logging.info("Mainnet Tier-S shadow runtime stopped.")
    finally:
        lock.close()


if __name__ == "__main__":
    main()
