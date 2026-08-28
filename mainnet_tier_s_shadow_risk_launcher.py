"""Canonical Entry Edge, Guardian-risk and persistence hardening wrapper."""
import asyncio
import logging
import time

import mainnet_tier_s_shadow_launcher as base

risk = base.app.load_module(
    "shadow_risk_guard_runtime",
    base.app.CURRENT_DIR / "loi_he_thong" / "shadow_risk_guard.py",
)
edge = base.app.load_module(
    "entry_edge_tier_runtime",
    base.app.CURRENT_DIR / "loi_he_thong" / "entry_edge_tier.py",
)
health = base.app.load_module(
    "shadow_runtime_health_runtime",
    base.app.CURRENT_DIR / "loi_he_thong" / "shadow_runtime_health.py",
)
runtime_state = base.app.load_module(
    "shadow_runtime_state_runtime",
    base.app.CURRENT_DIR / "loi_he_thong" / "shadow_runtime_state.py",
)
base.app.state.wstrade_runtime_state_save = lambda: runtime_state.save(base)

futures_flow = base.app.load_module(
    "futures_flow_hardening_runtime",
    base.app.CURRENT_DIR / "loi_he_thong" / "futures_flow_hardening.py",
)

_orig_open = base._open_shadow
_orig_account_init = base._shadow_account_init
_orig_assess = risk.assess
_last_persist = 0.0

_ENTRY_HISTORY_FIELDS = (
    "_ignition_episode",
)

def _reset_entry_context(state, next_side, reason, now):
    cleared = 0
    for name in _ENTRY_HISTORY_FIELDS:
        hist = getattr(state, name, None)
        if name == "_ignition_episode":
            setattr(state, name, None)
        elif hist is not None:
            try:
                cleared += len(hist)
                hist.clear()
            except (AttributeError, TypeError):
                setattr(state, name, None)
    state._entry_causal_context_side = next_side
    state._entry_flow_persistence_side = next_side
    state._entry_acceptance_signature = None
    state._entry_acceptance_since = 0.0
    state.entry_causal_reset_at = now
    state.entry_causal_reset_reason = reason
    state.entry_causal_reset_count = int(getattr(state, "entry_causal_reset_count", 0) or 0) + 1
    state.entry_causal_reset_samples = cleared
    # Keep streaming baselines warm, but mark every pre-reset 100 ms bucket as
    # observed so a bias flip/reconnect can never replay old flow as ignition.
    signal_engine = getattr(state, "_ignition_signal_engine", None)
    venues = getattr(signal_engine, "venues", {}) or {}
    state._ignition_seen_bucket = {
        name: int(venue.history[-1].get("bucket_start_ms", -1))
        for name, venue in venues.items() if getattr(venue, "history", None)
    }

def _flow_volume_quorum_required(state, now, required=2):
    required = max(1, int(required))
    floors = dict(getattr(base.entry_council, "MIN_VOL_BTC_BY_VENUE", {}) or {})
    venues = {}

    spot_ts = float(getattr(state, "thoi_gian_dong_tien_cuoi", 0.0) or 0.0)
    spot_vol = (
        float(getattr(state, "current_cvd_buy_3s", 0.0) or 0.0)
        + float(getattr(state, "current_cvd_sell_3s", 0.0) or 0.0)
    )
    if health.fresh(spot_ts, now, 5.0) and spot_vol >= float(floors.get("spot", 0.015)):
        venues["spot"] = spot_vol

    cb_ts = float(getattr(state, "coinbase_flow_3s_ts", 0.0) or 0.0)
    cb_vol = float(getattr(state, "coinbase_volume_3s", 0.0) or 0.0)
    if health.fresh(cb_ts, now, 5.0) and cb_vol >= float(floors.get("coinbase", 0.002)):
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
    if health.fresh(newest, now, 5.0) and fut_vol >= float(floors.get("futures", 0.15)):
        venues["futures"] = fut_vol

    state.entry_tier_s_volume_quality = {
        "floor_btc_by_venue": floors,
        "venues": venues,
        "required": required,
    }
    return len(venues) >= required

def _entry_quorum_ok(result, state, now):
    allowed, report = edge.authorize(result, state)
    state.entry_edge_tier = report
    state.entry_edge_class = report.get("edge_class")
    state.entry_edge_cost_ok = report.get("cost_ok")
    state.entry_edge_updated_at = now
    if not allowed:
        return False
    ignition = (result or {}).get("ignition") or {}
    current_cash = dict(ignition.get("current_cash_conversion") or {})
    state.entry_tier_s_volume_quality = {
        "source": "IGNITION_100MS_SNAPSHOT",
        "venues": dict(ignition.get("flow_by_venue") or {}),
        "cash_venues": list(ignition.get("cash_venues") or ()),
        "proof_type": ignition.get("proof_type"),
    }
    return bool(
        ignition.get("state") == "PROVE"
        and ignition.get("cash_venues")
        and current_cash.get("confirmed")
        and ignition.get("proof_type") in (
            "METAORDER_CONTINUATION", "FAILED_REVERSION",
            "PERSISTENT_METAORDER",
        )
        and (
            ignition.get("proposer") != "futures"
            or ignition.get("futures_cash_response_ok")
        )
    )

def _open_shadow(side, result, now):
    state = base.app.state
    report = getattr(state, "entry_edge_tier", None) or edge.classify(result, state)
    result = dict(result)
    result["edge_tier"] = report
    pos = _orig_open(side, result, now)
    if pos is not None:
        risk.arm(pos, pos.entry_price)
        # Keep the promotion sample geometrically identical to live: adaptive
        # 0.35%-0.55% exchange-stop distance, not the legacy fixed 0.55% model.
        hard_sl, _ = base.live_execution._risk_geometry(
            state, side, pos.entry_price
        )
        pos.hard_sl = hard_sl
        pos.r = abs(float(pos.entry_price) - float(hard_sl))
        total_cost_bps = base.verified_cost_model.position_total_cost_bps(
            pos, fallback_bps=2.0 * float(risk.FEE_BPS)
        )
        pos.fee_r = (
            float(pos.entry_price) * total_cost_bps / 10000.0
        ) / max(float(pos.r), 1e-12)
        state.mainnet_shadow_risk = risk.snap(pos, pos.entry_price)
        state.mainnet_shadow_entry_edge = report
        base._record_position_state(
            pos, {}, state.mainnet_shadow_risk, pos.entry_price, now, force=True
        )
        runtime_state.save(base)
    return pos

async def _account_init():
    # app.main() awaits account init; keep this wrapper async so the base shadow
    # initializer actually runs before persistence recovery.
    await _orig_account_init()
    restored = runtime_state.restore(base)
    state = base.app.state
    for name, default in (
        ("mainnet_shadow_realized_pnl", 0.0),
        ("mainnet_shadow_trades", 0),
        ("mainnet_shadow_wins", 0),
        ("mainnet_shadow_losses", 0),
        ("mainnet_shadow_breakevens", 0),
        ("mainnet_shadow_gross_profit", 0.0),
        ("mainnet_shadow_gross_loss", 0.0),
        ("mainnet_shadow_stress_25bps_pnl", 0.0),
    ):
        if not hasattr(state, name):
            setattr(state, name, default)
    state.mainnet_shadow_restore_ok = bool(restored)
    base.shadow_daily_loss.initialize(
        state,
        now=time.time(),
        restored=bool(restored),
        checkpoint_ts=float(
            getattr(state, "mainnet_shadow_checkpoint_ts", 0.0) or 0.0
        ),
        limit=base.DAILY_LOSS_USDT,
        enforce=base.SHADOW_DAILY_LOSS_ENFORCED,
    )
    pos = getattr(state, "mainnet_shadow_position", None)
    if restored and pos is not None and bool(getattr(pos, "active", False)):
        entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
        r_value = float(getattr(pos, "r", 0.0) or 0.0)
        hard_sl = float(getattr(pos, "hard_sl", 0.0) or 0.0)
        core_ready = entry > 0.0 and r_value > 0.0 and hard_sl > 0.0 and hasattr(pos, "best")
        if not core_ready:
            # V1 snapshots used wrong risk-field names. Re-arm from entry rather than
            # pretending the ratchet survived; mark the recovery degraded for audit.
            risk.arm(pos, entry)
            state.mainnet_shadow_recovery_degraded = True
            state.mainnet_shadow_recovery_reason = "RISK_STATE_INCOMPLETE_REARMED"
        else:
            total_cost_bps = base.verified_cost_model.position_total_cost_bps(
                pos, fallback_bps=2.0 * float(risk.FEE_BPS)
            )
            required_fee_r = (entry * total_cost_bps / 10000.0) / r_value
            pos.fee_r = max(float(getattr(pos, "fee_r", 0.0) or 0.0), required_fee_r)
            if not hasattr(pos, "stage"):
                pos.stage = "INITIAL"
            if not hasattr(pos, "tier_mode"):
                pos.tier_mode = "PROTECT"
        # Recompute the tick-rounded hard bound with the current canonical
        # geometry. Older checkpoints may have rounded a 35 bps stop toward
        # entry by one tick, which breaks shadow/live parity.
        normalized_sl, normalized_plan = base.live_execution._risk_geometry(
            state, pos.side, entry
        )
        current_pct = abs(entry - float(getattr(pos, "hard_sl", 0.0) or 0.0)) / entry
        if normalized_plan.get("eligible") and not (
            0.0035 - 1e-12 <= current_pct <= 0.0055 + 1e-12
        ):
            pos.hard_sl = normalized_sl
            pos.r = abs(entry - normalized_sl)
            direction = 1.0 if pos.side == "LONG" else -1.0
            pos.best_r = max(
                0.0, direction * (float(getattr(pos, "best", entry)) - entry)
                / max(float(pos.r), 1e-12)
            )
            if getattr(pos, "floor", None) is not None:
                pos.floor_r = direction * (float(pos.floor) - entry) / max(
                    float(pos.r), 1e-12
                )
            total_cost_bps = base.verified_cost_model.position_total_cost_bps(
                pos, fallback_bps=2.0 * float(risk.FEE_BPS)
            )
            pos.fee_r = (
                entry * total_cost_bps / 10000.0
            ) / max(float(pos.r), 1e-12)
            state.mainnet_shadow_recovery_degraded = True
            state.mainnet_shadow_recovery_reason = "HARD_SL_TICK_ROUNDING_NORMALIZED"
        runtime_state.save(base)

def _assess_and_persist(pos, px, guardian, market_state=None, now=None):
    global _last_persist
    out = _orig_assess(pos, px, guardian, market_state=market_state, now=now)
    t = time.time() if now is None else float(now)
    if t - _last_persist >= 1.0:
        runtime_state.save(base)
        _last_persist = t
    return out

async def _bias_loop():
    while True:
        try:
            s = base.app.state
            result = base.bias_council.update_state(s, now=time.time())
            s.bias_council = result
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] canonical Tier-S bias loop failure")
            await asyncio.sleep(0.50)
        await asyncio.sleep(base.BIAS_SCOUT)

# Install core hardening first.
risk.assess = _assess_and_persist
base._shadow_account_init = _account_init
base._entry_quorum_ok = _entry_quorum_ok
base._open_shadow = _open_shadow
base._bias_loop = _bias_loop

# Keep a conservative fallback for missing authenticated commission data.  When
# Binance commission is verified, each position carries its actual maker/taker
# cost plan and both shadow PnL and Risk use that same plan.
base.SHADOW_FEE_BPS_PER_SIDE = max(float(getattr(base, "SHADOW_FEE_BPS_PER_SIDE", 0.0) or 0.0), 9.0)
risk.FEE_BPS = base.SHADOW_FEE_BPS_PER_SIDE
_cost_close = base._close_shadow
def _close_and_persist(pos, result, now):
    state = base.app.state
    was_active = bool(getattr(pos, "active", False))
    before = float(getattr(state, "mainnet_shadow_balance_usdt", 0.0) or 0.0)
    out = _cost_close(pos, result, now)
    closed = was_active and not bool(getattr(pos, "active", False))
    if closed:
        after = float(getattr(state, "mainnet_shadow_balance_usdt", before) or before)
        net = after - before
        state.mainnet_shadow_realized_pnl = float(
            getattr(state, "mainnet_shadow_realized_pnl", 0.0) or 0.0
        ) + net
        state.mainnet_shadow_trades = int(getattr(state, "mainnet_shadow_trades", 0) or 0) + 1
        if net > 1e-12:
            state.mainnet_shadow_wins = int(getattr(state, "mainnet_shadow_wins", 0) or 0) + 1
            state.mainnet_shadow_gross_profit = float(
                getattr(state, "mainnet_shadow_gross_profit", 0.0) or 0.0
            ) + net
        elif net < -1e-12:
            state.mainnet_shadow_losses = int(getattr(state, "mainnet_shadow_losses", 0) or 0) + 1
            state.mainnet_shadow_gross_loss = float(
                getattr(state, "mainnet_shadow_gross_loss", 0.0) or 0.0
            ) + abs(net)
        else:
            state.mainnet_shadow_breakevens = int(
                getattr(state, "mainnet_shadow_breakevens", 0) or 0
            ) + 1
        state.mainnet_shadow_last_net_pnl = net
        state.mainnet_shadow_last_closed_at = float(now)
        base.shadow_daily_loss.record_close(
            state, net, now=now, limit=base.DAILY_LOSS_USDT,
            enforce=base.SHADOW_DAILY_LOSS_ENFORCED,
        )
    runtime_state.save(base)
    return out
base._close_shadow = _close_and_persist

# If the data orchestrator ever returns normally, do not leave the infinite shadow
# loops alive with permanently stale feeds. Let the top-level gather fail so systemd restarts.
_orig_app_main = base.app.main
async def _app_main_failfast(*args, **kwargs):
    await _orig_app_main(*args, **kwargs)
    raise RuntimeError("SHADOW_DATA_ORCHESTRATOR_EXITED_UNEXPECTEDLY")
base.app.main = _app_main_failfast

# Install local-time / time-bounded Spot/Futures flow before app.main() creates collectors.
futures_flow.install(base)

# Reconnect-safe Entry refs, shadow0readiness, macro-fresh Guardian, and
# risk-first Guardian loop (hard SL/profit floor survive stale Spot).
_health_probe = health.install(base, risk, edge)

# Preserve bias-side handoff reset on top of the health-gated evaluator.
_health_eval = base.entry_council.evaluate
def _entry_evaluate_context_guard(state, now=None, side=None):
    now = time.time() if now is None else float(now)
    if bool(getattr(state, "futures_flow_ring_saturated", False)):
        previous = str(getattr(state, "_entry_causal_context_side", "ABSTAIN") or "ABSTAIN").upper()
        if previous in ("LONG", "SHORT"):
            _reset_entry_context(state, "ABSTAIN", "FUTURES_FLOW_RING_SATURATED", now)
        state.mainnet_shadow_ready = False
        state.system_ready = False
        state.last_readiness_reason = "SHADOW_FEED_DEGRADED:futures_flow_ring_saturated"
        return {
            "version": getattr(base.entry_council, "VERSION", "ENTRY"),
            "decision": "WAIT",
            "entry_mode": "NONE",
            "phase": "ARMED",
            "confidence": 0.0,
            "reason": "FUTURES_FLOW_RING_SATURATED",
            "side": str(side or getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper(),
            "s_votes": {},
            "ts": now,
        }
    current = str(side or getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
    conf = float(getattr(state, "bias_confidence", 0.0) or 0.0)
    bias_ts = float(getattr(state, "bias_updated_at", 0.0) or 0.0)
    min_conf = float(getattr(base.entry_council, "BIAS_MIN_CONF", 0.55))
    max_age = float(getattr(base.entry_council, "BIAS_MAX_AGE", 3.0))
    valid = current in ("LONG", "SHORT") and conf >= min_conf and health.fresh(bias_ts, now, max_age)
    previous = str(getattr(state, "_entry_causal_context_side", "ABSTAIN") or "ABSTAIN").upper()
    if not valid:
        if previous in ("LONG", "SHORT"):
            _reset_entry_context(state, "ABSTAIN", "BIAS_INVALID_OR_EXPIRED", now)
    elif previous != current:
        reason = "BIAS_SIDE_CHANGE" if previous in ("LONG", "SHORT") else "BIAS_ACQUIRE"
        _reset_entry_context(state, current, reason, now)
    return _health_eval(state, now=now, side=side)

base.entry_council.evaluate = _entry_evaluate_context_guard

if __name__ == "__main__":
    base.main()
