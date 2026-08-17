"""Mainnet shadow wrapper with Tier-S adaptive SL/TP/profit protection and entry edge gating."""
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

_orig_open = base._open_shadow
_orig_latest_futures_price = base._latest_futures_price

def _shadow_exec_price(now=None):
    """Prefer fresh Mainnet Futures BBO; fall back to public Futures trade, then Spot."""
    now = time.time() if now is None else float(now)
    state = base.app.state
    ts = float(getattr(state, "execution_price_time", 0.0) or 0.0)
    bid = float(getattr(state, "execution_best_bid", 0.0) or 0.0)
    ask = float(getattr(state, "execution_best_ask", 0.0) or 0.0)
    if ts > 0.0 and 0.0 <= now - ts <= 5.0 and bid > 0.0 and ask > bid:
        return (bid + ask) / 2.0
    return _orig_latest_futures_price(now)

_orig_entry_evaluate = base.entry_council.evaluate

_ENTRY_HISTORY_FIELDS = (
    "entry_shadow_price_history",
    "entry_causal_flow_history",
)

def _reset_entry_causal_context(state, next_side, reason, now):
    """Discard side-signed causal evidence whenever the directional context changes."""
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

def _entry_evaluate_context_guard(state, now=None, side=None):
    """Keep causal entry history pure to one fresh, confident bias side."""
    now = time.time() if now is None else float(now)
    current = str(side or getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
    confidence = float(getattr(state, "bias_confidence", 0.0) or 0.0)
    bias_ts = float(getattr(state, "bias_updated_at", 0.0) or 0.0)
    min_conf = float(getattr(base.entry_council, "BIAS_MIN_CONF", 0.55))
    max_age = float(getattr(base.entry_council, "BIAS_MAX_AGE", 3.0))
    valid = (
        current in ("LONG", "SHORT")
        and confidence >= min_conf
        and bias_ts > 0.0
        and 0.0 <= now - bias_ts <= max_age
    )
    previous = str(getattr(state, "_entry_causal_context_side", "ABSTAIN") or "ABSTAIN").upper()

    if not valid:
        if previous in ("LONG", "SHORT"):
            _reset_entry_causal_context(state, "ABSTAIN", "BIAS_INVALID_OR_EXPIRED", now)
    elif previous != current:
        reason = "BIAS_SIDE_CHANGE" if previous in ("LONG", "SHORT") else "BIAS_ACQUIRE"
        _reset_entry_causal_context(state, current, reason, now)

    return _orig_entry_evaluate(state, now=now, side=side)

def _flow_volume_quorum_required(state, now, required=2):
    """Same material-flow floor as base, with an explicit FAST/NORMAL venue count."""
    required = max(1, int(required))
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
    if spot_ts > 0.0 and 0.0 <= now - spot_ts <= 5.0 and spot_vol >= floor:
        venues["spot"] = spot_vol

    cb_ts = float(getattr(state, "coinbase_flow_3s_ts", 0.0) or 0.0)
    cb_vol = float(getattr(state, "coinbase_volume_3s", 0.0) or 0.0)
    if cb_ts > 0.0 and 0.0 <= now - cb_ts <= 5.0 and cb_vol >= floor:
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
    if newest > 0.0 and 0.0 <= now - newest <= 5.0 and fut_vol >= floor:
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
    if report.get("entry_mode") == "FAST":
        # FAST deliberately permits one strong material flow venue, but never stale/tiny flow.
        return _flow_volume_quorum_required(state, now, required=1)
    # NORMAL keeps the stricter two-venue material/fresh flow floor.
    return _flow_volume_quorum_required(state, now, required=2)

def _open_shadow(side, result,now):
    state = base.app.state
    report = getattr(state, "entry_edge_tier", None) or edge.classify(result, state)
    result = dict(result)
    result["edge_tier"] = report
    pos = _orig_open(side, result, now)
    if pos is not None:
        risk.arm(pos, pos.entry_price)
        state.mainnet_shadow_risk = risk.snap(pos, pos.entry_price)
        state.mainnet_shadow_entry_edge = report
        logging.info(
            "[MAINNET-SHADOW] ENTRY edge=%s costx=%.2f fast=%s",
            report.get("edge_class"),
            float(report.get("cost_multiple_model") or 0.0),
            bool(report.get("fast_contract_ok")),
        )
    return pos


async def _bias_loop():
    """Run the direction-only Bias Council through update_state so hysteresis is live."""
    while True:
        try:
            s = base.app.state
            result = base.bias_council.update_state(s, now=time.time())
            s.bias_council = result
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] causal Tier-S bias loop failure")
            await asyncio.sleep(0.50)
        await asyncio.sleep(base.BIAS_SCOUT)

async def _guardian_loop():
    while True:
        try:
            s = base.app.state
            pos = getattr(s, "mainnet_shadow_position", None)
            if pos is None or not bool(getattr(pos, "active", False)):
                await asyncio.sleep(0.10)
                continue
            now = time.time()
            if not base._spot_fresh(now):
                s.guardian_s_decision = "HOLD_STALE_SPOT"
                await asyncio.sleep(base.GUARD_POLL)
                continue

            px = base._latest_futures_price(now)
            guardian = base.guardian_s.update_state(s, pos, now=now)

            rr = risk.assess(pos, px, guardian, market_state=s, now=now)
            s.mainnet_shadow_risk = rr
            s.mainnet_shadow_tier_mode = rr.get("tier_mode")
            s.mainnet_shadow_tier_supportive = rr.get("supportive_count", 0)
            s.mainnet_shadow_tier_adverse = rr.get("adverse_count", 0)

            if rr.get("decision") == "EXIT":
                base._close_shadow(
                    pos,
                    {"decision": "EXIT", "reason": rr["reason"], "risk": rr, "guardian": guardian},
                    now,
                )
                await asyncio.sleep(base.GUARD_POLL)
                continue

            if guardian.get("decision") == "EXIT" and risk.guardian_ok(guardian):
                base._close_shadow(pos, guardian, now)
            elif guardian.get("decision") == "EXIT":
                s.guardian_s_decision = "WATCH_CAUSAL_GATE"
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] Tier-S protected guardian failure")
            await asyncio.sleep(0.25)
        await asyncio.sleep(base.GUARD_POLL)

base._latest_futures_price = _shadow_exec_price
base.entry_council.evaluate = _entry_evaluate_context_guard
base._entry_quorum_ok = _entry_quorum_ok
base._bias_loop = _bias_loop
base._open_shadow = _open_shadow
base._guardian_loop = _guardian_loop

if __name__ == "__main__":
    base.main()
