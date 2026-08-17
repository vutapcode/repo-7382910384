"""Mainnet Tier-S shadow runtime hardening wrapper."""
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

_orig_open = base._open_shadow
_orig_account_init = base._shadow_account_init
_orig_assess = risk.assess
_last_persist = 0.0

_ENTRY_HISTORY_FIELDS = (
    "entry_shadow_price_history",
    "entry_causal_flow_history",
)

def _reset_entry_context(state, next_side, reason, now):
    cleared = 0
    for name in _ENTRY_HISTORY_FIELDS:
        hist = getattr(state, name, None)
        if hist is not None:
            try:
                cleared += len(hist)
                hist.clear()
            except (AttributeError, TypeError):
                setattr(state, name, None)
    state._entry_causal_context_side = next_side
    state.entry_causal_reset_at = now
    state.entry_causal_reset_reason = reason
    state.entry_causal_reset_count = int(getattr(state, "entry_causal_reset_count", 0) or 0) + 1
    state.entry_causal_reset_samples = cleared

def _flow_volume_quorum_required(state, now, required=2):
    required = max(1, int(required))
    floor = max(0.02, min(0.10, 0.02 * float(getattr(state, "vol_pct90", 0.0) or 0.0)))
    venues = {}

    spot_ts = float(getattr(state, "thoi_gian_dong_tien_cuoi", 0.0) or 0.0)
    spot_vol = (
        float(getattr(state, "current_cvd_buy_3s", 0.0) or 0.0)
        + float(getattr(state, "current_cvd_sell_3s", 0.0) or 0.0)
    )
    if health.fresh(spot_ts, now, 5.0) and spot_vol >= floor:
        venues["spot"] = spot_vol

    cb_ts = float(getattr(state, "coinbase_flow_3s_ts", 0.0) or 0.0)
    cb_vol = float(getattr(state, "coinbase_volume_3s", 0.0) or 0.0)
    if health.fresh(cb_ts, now, 5.0) and cb_vol >= floor:
        venues["coinbase"] = cb_vol

    cutoff = (now - 3.0) * 1000.0
    fut_vol = 0.0
    newest = 0.0
    for row in list(getattr(state, "danh_sach_khop_lenh_futures", ()) or ()):
        try:
            ts_ms = float(row.get("thoi_gian_ms", 0.0) or 0.0)
            if ts_ms < cutoff:
                continue
            fut_vol += float(row.get("khoi_luong", 0.0) or 0.0)
            newest = max(newest, ts_ms / 1000.0)
        except (AttributeError, TypeError, ValueError):
            continue
    if health.fresh(newest, now, 5.0) and fut_vol >= floor:
        venues["futures"] = fut_vol

    state.entry_tier_s_volume_quality = {
        "floor_btc": floor,
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
    required = 1 if report.get("entry_mode") == "FAST" else 2
    return _flow_volume_quorum_required(state, now, required=required)

def _open_shadow(side, result, now):
    state = base.app.state
    report = getattr(state, "entry_edge_tier", None) or edge.classify(result, state)
    result = dict(result)
    result["edge_tier"] = report
    pos = _orig_open(side, result, now)
    if pos is not None:
        risk.arm(pos, pos.entry_price)
        state.mainnet_shadow_risk = risk.snap(pos, pos.entry_price)
        state.mainnet_shadow_entry_edge = report
        runtime_state.save(base)
    return pos

def _account_init():
    _orig_account_init()
    restored = runtime_state.restore(base)
    base.app.state.mainnet_shadow_restore_ok = bool(restored)

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

# Force native shadow accounting to the same conservative 18bps round-trip budget,
# even when launched manually outside systemd (9bps modeled all-in per side).
base.SHADOW_FEE_BPS_PER_SIDE = max(float(getattr(base, "SHADOW_FEE_BPS_PER_SIDE", 0.0) or 0.0), 9.0)
runtime_state.install_cost_accounting(base, model_cost_bps=18.0)
_cost_close = base._close_shadow
def _close_and_persist(pos, result, now):
    out = _cost_close(pos, result, now)
    runtime_state.save(base)
    return out
base._close_shadow = _close_and_persist

# Reconnect-safe Entry refs, shadow readiness, macro-fresh Guardian, and
# risk-first Guardian loop (hard SL/profit floor survive stale Spot).
_health_probe = health.install(base, risk, edge)

# Preserve bias-side handoff reset on top of the health-gated evaluator.
_health_eval = base.entry_council.evaluate
def _entry_eval(state, now=None, side=None):
    now = time.time() if now is None else float(now)
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

base.entry_council.evaluate = _entry_eval

if __name_ == "__main__":
    base.main()
