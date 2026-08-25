"""Canonical Entry causal hardening; installed by the lean launcher."""
import time

VERSION = "BIAS_OI_FRESHNESS_HOOK_V3_ENTRY_CAUSAL"
MAX_OI_AGE_S = 18.0
IGNITION_OI_MAX_AGE_S = 6.0
ATR_MAX_AGE_S = 120.0
LIVE_MAX_AGE_S = 2.5
_INSTALLED = False


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _opponent(core, row, side):
    return (
        str(row.get("side") or "").upper() != str(side or "").upper()
        and core._material_flow(row)
    )


def _futures_reversal(core, histories, episode, now_ms):
    side = str(episode.get("side") or "").upper()
    start = int(episode.get("started_receive_ms", 0) or 0)
    rows = [
        r for r in histories.get("futures", ())
        if start <= int(r.get("receive_time_ms", 0) or 0) <= now_ms
        and core._material_flow(r)
    ]
    aligned = [r for r in rows if str(r.get("side") or "").upper() == side]
    if not aligned:
        return None
    first = min(int(r.get("receive_time_ms", 0) or 0) for r in aligned)
    opposite = [
        r for r in rows
        if int(r.get("receive_time_ms", 0) or 0) > first
        and _opponent(core, r, side)
    ]
    if not opposite:
        return None
    r = opposite[-1]
    return {
        "version": "FUTURES_CAUSAL_REVERSAL_V1",
        "first_follow_receive_ms": first,
        "reversal_receive_ms": int(r.get("receive_time_ms", 0) or 0),
        "reversal_side": str(r.get("side") or "").upper(),
        "reversal_volume_btc": round(_f(r.get("total_qty")), 8),
        "reversal_imbalance": round(_f(r.get("imbalance")), 6),
    }


def _revalidate(state, now=None):
    from loi_he_thong import ignition_core as core, ignition_signals as signals
    now = time.time() if now is None else float(now)
    rid = int(getattr(state, "canonical_reserved_opportunity_id", 0) or 0)
    ctx = dict(getattr(state, "canonical_reserved_context", {}) or {})
    out = {"version": "LIVE_CAUSAL_REVALIDATION_V1", "reserved_id": rid}
    if rid <= 0 or int(ctx.get("opportunity_id", 0) or 0) != rid:
        return False, "LIVE_CAUSAL_RESERVATION_MISSING", out
    active = int(getattr(state, "canonical_opportunity_count", 0) or 0)
    episode = getattr(state, "canonical_opportunity_active_episode_id", None)
    if active != rid or (ctx.get("causal_episode_id") and episode != ctx.get("causal_episode_id")):
        return False, "LIVE_CAUSAL_EPISODE_CHANGED", out
    side = str(ctx.get("side") or "").upper()
    bias = str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
    bts = _f(getattr(state, "bias_updated_at", 0.0))
    if (
        side not in ("LONG", "SHORT") or bias != side
        or _f(getattr(state, "bias_confidence", 0.0)) < core.BIAS_MIN_CONF
        or bts <= 0.0 or not 0.0 <= now - bts <= core.BIAS_MAX_AGE
    ):
        return False, "LIVE_CAUSAL_BIAS_CHANGED", out
    memory = ((getattr(state, "bias_council", None) or {}).get("direction_memory") or {})
    candidate = str(memory.get("candidate_side") or "ABSTAIN").upper()
    if str(memory.get("phase") or "").upper() == "REVERSAL_CANDIDATE" and candidate in ("LONG", "SHORT") and candidate != side:
        return False, "LIVE_CAUSAL_BIAS_REVERSAL_PENDING", out
    rts = _f(ctx.get("result_ts"))
    if rts <= 0.0 or not 0.0 <= now - rts <= LIVE_MAX_AGE_S:
        return False, "LIVE_CAUSAL_PROOF_STALE", out
    current = dict(getattr(state, "entry_shadow_council", {}) or {})
    current_ep = current.get("causal_episode_id") or (current.get("ignition") or {}).get("causal_episode_id")
    if current_ep and current_ep != ctx.get("causal_episode_id"):
        return False, "LIVE_CAUSAL_COUNCIL_EPISODE_CHANGED", out
    live = signals.engine(state).venues
    for venue, epoch in dict(ctx.get("epochs") or {}).items():
        if int(getattr(live.get(venue), "epoch", 0) or 0) != int(epoch or 0):
            return False, "LIVE_CAUSAL_DATA_GAP_OR_EPOCH_RESET", out
    fresh = core._freshness(state, now)
    if not fresh.get("binance_spot_ready") or not fresh.get("futures_ready") or str(fresh.get("coinbase_mode") or "").upper() == "STALE":
        return False, "LIVE_CAUSAL_FEED_STALE", out
    bid, ask = _f(getattr(state, "execution_best_bid", 0.0)), _f(getattr(state, "execution_best_ask", 0.0))
    bbo_ts = _f(getattr(state, "execution_price_time", 0.0))
    if bid <= 0.0 or ask <= bid or bbo_ts <= 0.0 or not 0.0 <= now - bbo_ts <= LIVE_MAX_AGE_S:
        return False, "LIVE_CAUSAL_BBO_STALE", out
    histories = signals.snapshot(state, int(now * 1000.0))
    cutoff = int(rts * 1000.0)
    for venue in ("binance_spot", "coinbase_spot", "futures"):
        for row in histories.get(venue, ()):
            if int(row.get("receive_time_ms", 0) or 0) > cutoff and _opponent(core, row, side):
                return False, "LIVE_CAUSAL_OPPOSING_FLOW", out
    return True, "PASS", out


def _install_ignition():
    from loi_he_thond import ignition_core as core
    if getattr(core, "_entry_causal_hardening_v1", False):
        return
    remember0, phase0, result0 = core._remember_bias, core._phase_measurement, core._result_from_episode

    def remember(state, now=None):
        remember0(state, now)
        hist = getattr(state, "_ignition_bias_snapshots", None)
        if not hist:
            return
        row = hist[-1]
        ctx = dict(row.get("direction_context") or {})
        if ctx.get("oi_snapshot_frozen"):
            return
        ctx["oi_updated_at"] = max(
            _f(getattr(state, "open_interest_updated_at", 0.0)),
            _f(getattr(state, "thoi_gian_vi_mo_cuoi", 0.0)),
        )
        ctx["oi_value"] = _f(getattr(state, "open_interest", 0.0))
        ctx["oi_snapshot_frozen"] = True
        row["direction_context"] = ctx

    def oi_intent(state, side, now, bias_snapshot=None):
        frozen = (bias_snapshot or {}).get("direction_context") or {}
        regime = str(frozen.get("oi_regime") or "UNKNOWN").upper()
        ots = _f(frozen.get("oi_updated_at"))
        age = now - ots if ots > 0.0 else float("inf")
        fresh = ots > 0.0 and 0.0 <= age <= IGNITION_OI_MAX_AGE_S
        intent = "UNWIND" if regime in ("SHORT_COVERING", "LONG_LIQUIDATION_CLOSING") else "POSITION_BUILD" if regime in ("NEW_LONG_BUILD", "NEW_SHORT_BUILD") else "NEUTRAL"
        expected = {"SHORT_COVERING":"LONG","LONG_LIQUIDATION_CLOSING":"SHORT","NEW_LONG_BUILD":"LONG","NEW_SHORT_BUILD":"SHORT"}.get(regime)
        aligned = expected in (None, str(side).upper())
        return {
            "intent": intent, "raw_regime": regime, "fresh": fresh,
            "age_seconds": round(max(0.0, age), 4) if age != float("inf") else None,
            "snapshot_updated_at": ots or None, "side": side,
            "expected_side": expected, "aligned_with_entry": aligned,
            "causal_class": "OI_STALE_CONTEXT" if not fresh else "OI_DIRECTION_CONFLICT" if intent != "NEUTRAL" and not aligned else "ALIGNED_BUILD" if intent == "POSITION_BUILD" else "CASH_LED_UNWIND" if intent == "UNWIND" else "OI_NEUTRAL",
        }

    def phase(state, side, cash_venues, venue_moves, latest):
        row = dict(phase0(state, side, cash_venues, venue_moves, latest) or {})
        now = _f(getattr(state, "_entry_causal_eval_now", 0.0), time.time())
        ats = _f(getattr(state, "atr_1m_updated_at", 0.0))
        age = now - ats if ats > 0.0 else float("inf")
        fresh = ats > 0.0 and 0.0 <= age <= ATR_MAX_AGE_S
        row.update({"atr_updated_at": ats or None, "atr_age_seconds": round(max(0.0, age), 4) if age != float("inf") else None, "atr_fresh": fresh})
        if row.get("valid") and not fresh:
            row.update({"valid": False, "source": "ATR_1M_STALE", "consumed_fraction": 1.0})
        return row

    def result(state, episode, histories, freshness, now):
        rev = _futures_reversal(core, histories, episode, int(float(now) * 1000.0))
        if rev is not None:
            state._ignition_episode = None
            state._ignition_last_reject = "OPPOSING_FUTURES_FLOW"
            payload = {"causal_episode_id": episode.get("causal_episode_id"), "side": episode.get("side"), "state": "INVALID", "futures_follow_ok": False, "futures_flow_reversed": True, "futures_reversal": rev}
            return core._wait(now, episode.get("side"), "OPPOSING_FUTURES_FLOW", "INVALID", payload, freshness)
        old = getattr(state, "_entry_causal_eval_now", None)
        state._entry_causal_eval_now = float(now)
        try:
            return result0(state, episode, histories, freshness, now)
        finally:
            if old is None:
                try: delattr(state, "_entry_causal_eval_now")
                except AttributeError: pass
            else:
                state._entry_causal_eval_now = old

    core._remember_bias, core._oi_intent = remember, oi_intent
    core._phase_measurement, core._result_from_episode = phase, result
    core._entry_causal_hardening_v1 = True


def _install_live_gate():
    from loi_he_thong import canonical_opportunity as canonical, mainnet_safety as safety
    if getattr(safety, "_entry_causal_revalidation_v1", False):
        return
    gate0 = safety.exchange_entry_gate

    async def gate(api, state, entry_price, hard_sl, risk_plan=None):
        ok, reason, details = await gate0(api, state, entry_price, hard_sl, risk_plan=risk_plan)
        rid = int(getattr(state, "canonical_reserved_opportunity_id", 0) or 0)
        if not ok:
            if rid:
                canonical.release(state, rid, reason=reason)
            return ok, reason, details
        if not safety.is_mainnet(api) or not bool(getattr(state, "wstrade_live_armed", False)):
            return ok, reason, details
        ok2, reason2, causal = _revalidate(state)
        merged = dict(details or {})
        merged["causal_revalidation"] = causal
        if not ok2:
            if rid:
                canonical.release(state, rid, reason=reason2)
            return False, reason2, merged
        return True, "PASS", merged

    safety.exchange_entry_gate = gate
    safety._entry_causal_revalidation_v1 = True


def _install_shadow_release():
    import mainnet_tier_s_shadow_launcher as shadow
    from loi_he_thong import canonical_opportunity as canonical
    if getattr(shadow, "_canonical_reservation_release_v1", False):
        return
    advance0 = shadow._advance_shadow_pending

    def advance(now):
        pending = getattr(shadow.app.state, "mainnet_shadow_pending_entry", None)
        oid = int(((pending or {}).get("result") or {}).get("canonical_opportunity_id", 0) or 0) if isinstance(pending, dict) else 0
        position = advance0(now)
        after = getattr(shadow.app.state, "mainnet_shadow_pending_entry", None)
        if oid and position is None and not isinstance(after, dict):
            canonical.release(shadow.app.state, oid, reason="MAKER_PENDING_TERMINAL_NO_FILL")
        return position

    shadow._advance_shadow_pending = advance
    shadow._canonical_reservation_release_v1 = True


def install(bias_module):
    global _INSTALLED
    bias_module.OI_AGE = MAX_OI_AGE_S
    bias_module.BIAS_OI_FRESHNESS_POLICY = VERSION
    if not _INSTALLED:
        _install_ignition()
        _install_live_gate()
        _install_shadow_release()
        _INSTALLED = True
    return MAX_OI_AGE_S
