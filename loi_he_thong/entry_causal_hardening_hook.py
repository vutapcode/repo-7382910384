"""Hardening for the active Ignition -> execution handoff."""
import time

ATR_MAX_AGE_SECONDS = 120.0
BIAS_MAX_AGE_SECONDS = 3.0
BBO_MAX_AGE_SECONDS = 1.0
SUBMIT_MAX_AGE_SECONDS = 1.5

def _f(v, d=0.0):
    try: return float(v)
    except (TypeError, ValueError): return d

def _state(shadow):
    return shadow.app.s

def _oid(result):
    try: return int((result or {}).get("canonical_opportunity_id", 0) or 0)
    except (TypeError, ValueError): return 0

def _release(shadow, result):
    oid = _oid(result)
    if oid > 0:
        shadow.canonical_opportunity.release(_state(shadow), oid)

def _material(entry, row):
    fn = getattr(entry, "_material_flow", None)
    return bool(callable(fn) and fn(row))

def _histories(entry, state, now):
    sig = getattr(entry, "ignition_signals", None)
    snap = getattr(sig, "snapshot", None)
    return snap(state, int(float(now) * 1000.0)) if callable(snap) else {}

def _new_opposing(entry, state, side, result, now):
    decision_ms = int(_f((result or {}).get("ts")) * 1000.0)
    if decision_ms <= 0:
        return None
    for venue, rows in _histories(entry, state, now).items():
        if venue not in ("binance_spot", "coinbase_spot", "futures"):
            continue
        for row in rows:
            if int(row.get("receive_time_ms", 0) or 0) <= decision_ms:
                continue
            if str(row.get("side") or "").upper() == str(side).upper():
                continue
            if _material(entry, row):
                return {
                    "venue": venue,
                    "receive_time_ms": int(row.get("receive_time_ms", 0) or 0),
                    "side": str(row.get("side") or ""),
                    "total_qty": _f(row.get("total_qty")),
                    "imbalance": _f(row.get("imbalance")),
                    "price_conversion_bps": _f(row.get("price_conversion_bps")),
                }
    return None

def _futures_reversal(entry, state, side, result, now):
    ignition = (result or {}).get("ignition") or {}
    response_ms = int(ignition.get("futures_response_ms", 0) or 0)
    if response_ms <= 0:
        return None
    newest = None
    for row in _histories(entry, state, now).get("futures", ()):
        if int(row.get("receive_time_ms", 0) or 0) <= response_ms:
            continue
        if str(row.get("side") or "").upper() == str(side).upper():
            continue
        if _material(entry, row):
            newest = row
    if newest is None:
        return None
    return {
        "receive_time_ms": int(newest.get("receive_time_ms", 0) or 0),
        "side": str(newest.get("side") or ""),
        "total_qty": _f(newest.get("total_qty")),
        "imbalance": _f(newest.get("imbalance")),
        "price_conversion_bps": _f(newest.get("price_conversion_bps")),
    }

def _validate(shadow, side, result, now=None, require_submit_age=True):
    state = _state(shadow)
    now = time.time() if now is None else float(now)
    result = result or {}
    side = str(side or "").upper()
    if side not in ("LONG", "SHORT"):
        return False, "SIDE_INVALID", {}
    active = getattr(state, "canonical_opportunity", None)
    oid = _oid(result)
    if oid <= 0 or not isinstance(active, dict):
        return False, "CANONICAL_OPPORTUNITY_MISSING", {}
    if int(active.get("opportunity_id", 0) or 0) != oid:
        return False, "CANONICAL_OPPORTUNITY_CHANGED", {}
    cid = str(result.get("causal_episode_id") or "")
    if cid and str(active.get("causal_episode_id") or "") != cid:
        return False, "CAUSAL_EPISODE_CHANGED", {}
    if str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper() != side:
        return False, "BIAS_SIDE_CHANGED", {}
    if _f(getattr(state, "bias_confidence", 0.0)) < 0.55:
        return False, "BIAS_CONFIDENCE_DROPPED", {}
    bias_age = now - _f(getattr(state, "bias_updated_at", 0.0))
    if bias_age < 0.0 or bias_age > BIAS_MAX_AGE_SECONDS:
        return False, "BIAS_STALE", {"age_seconds": round(max(0.0, bias_age), 6)}
    frozen = ((result.get("ignition") or {}).get("bias_snapshot") or {})
    if frozen and str(frozen.get("direction") or "").upper() != side:
        return False, "FROZEN_BIAS_MISMATCH", {}
    decision_ts = _f(result.get("ts"))
    decision_age = now - decision_ts
    if require_submit_age and (decision_ts <= 0.0 or decision_age < 0.0 or decision_age > SUBMIT_MAX_AGE_SECONDS):
        return False, "CAUSAL_PROOF_STALE", {"age_seconds": round(max(0.0, decision_age), 6)}
    bid = _f(getattr(state, "execution_best_bid", 0.0))
    ask = _f(getattr(state, "execution_best_ask", 0.0))
    bbo_age = now - _f(getattr(state, "execution_price_time", 0.0))
    if bid <= 0.0 or ask <= bid:
        return False, "BBO_INVALID", {"bid": bid, "ask": ask}
    if bbo_age < 0.0 or bbo_age > BBO_MAX_AGE_SECONDS:
        return False, "BBO_STALE", {"age_seconds": round(max(0.0, bbo_age), 6)}
    entry = getattr(shadow, "entry_council", None)
    if entry is not None:
        rev = _futures_reversal(entry, state, side, result, now)
        if rev is not None:
            ignition = result.setdefault("ignition", {})
            ignition["futures_follow_ok"] = False
            ignition["futures_follow_invalidated"] = True
            ignition["futures_reversal_evidence"] = rev
            return False, "FUTURES_MATERIAL_REVERSAL", rev
        opp = _new_opposing(entry, state, side, result, now)
        if opp is not None:
            return False, "POST_PROOF_OPPOSING_FLOW", opp
    return True, "PASS", {
        "decision_age_seconds": round(max(0.0, decision_age), 6),
        "bbo_age_seconds": round(max(0.0, bbo_age), 6),
    }

def _patch_ignition(shadow):
    entry = getattr(shadow, "entry_council", None)
    if entry is None or getattr(entry, "_causal_hardening_installed", False):
        return
    original_remember = entry._remember_bias
    def remember_bias(state, now=None):
        hist = getattr(state, "_ignition_bias_snapshots", None)
        old_capture = _f(hist[-1].get("captured_at")) if hist is not None and len(hist) else None
        original_remember(state, now)
        hist = getattr(state, "_ignition_bias_snapshots", None)
        if hist is None or not len(hist): return
        row = hist[-1]
        if old_capture is not None and _f(row.get("captured_at")) == old_capture: return
        ctx = dict(row.get("direction_context") or {})
        ctx["oi_updated_at"] = _f(getattr(state, "open_interest_updated_at", 0.0))
        ctx["oi_value"] = _f(getattr(state, "open_interest", 0.0))
        ctx["oi_change_pct"] = _f(getattr(state, "open_interest_change_pct", 0.0))
        ctx["oi_change_window_seconds"] = _f(getattr(state, "open_interest_change_window_seconds", 0.0))
        row["direction_context"] = ctx
    original_oi = entry._oi_intent
    def oi_intent(state, side, now, bias_snapshot=None):
        report = dict(original_oi(state, side, now, bias_snapshot))
        frozen = ((bias_snapshot or {}).get("direction_context") or {})
        ts = _f(frozen.get("oi_updated_at"))
        age = float(now) - ts if ts > 0.0 else float("inf")
        report["frozen_oi_updated_at"] = ts or None
        report["frozen_oi_age_seconds"] = round(max(0.0, age), 4) if ts > 0.0 else None
        report["live_oi_updated_at"] = _f(getattr(state, "open_interest_updated_at", 0.0)) or None
        if report.get("intent_source") == "FROZEN_BIAS_OI_REGIME":
            fresh = bool(ts > 0.0 and 0.0 <= age <= 6.0)
            report["fresh"] = fresh
            if not fresh: report["causal_class"] = "OI_STALE_CONTEXT"
        return report
    original_phase = entry._phase_measurement
    def phase_measurement(state, side, cash_venues, venue_moves, latest, episode=None):
        report = dict(original_phase(state, side, cash_venues, venue_moves, latest, episode))
        event_now = _f((latest or {}).get("receive_time_ms")) / 1000.0
        if event_now <= 0.0: event_now = time.time()
        ts = _f(getattr(state, "atr_1m_updated_at", 0.0))
        age = event_now - ts if ts > 0.0 else float("inf")
        report["atr_updated_at"] = ts or None
        report["atr_age_seconds"] = round(max(0.0, age), 4) if ts > 0.0 else None
        report["atr_max_age_seconds"] = ATR_MAX_AGE_SECONDS
        if ts <= 0.0 or age < 0.0 or age > ATR_MAX_AGE_SECONDS:
            report["valid"] = False
            report["source"] = "ATR_1M_TIMESTAMP_UNAVAILABLE" if ts <= 0.0 else "ATR_1M_STALE"
            report["consumed_fraction"] = 1.0
        return report
    entry._remember_bias = remember_bias
    entry._oi_intent = oi_intent
    entry._phase_measurement = phase_measurement
    entry._causal_hardening_installed = True

def _patch_execution(shadow):
    if getattr(shadow, "_entry_causal_hardening_installed", False):
        return
    original_open = shadow._open_position
    async def open_position(side, result, now):
        state = _state(shadow)
        ok, reason, detail = _validate(shadow, side, result, now, True)
        state.entry_causal_revalidation = {"stage":"PRE_EXECUTION","ok":ok,"reason":reason,"detail":detail,"checked_at":float(now)}
        if not ok:
            _release(shadow, result)
            state.mainnet_shadow_last_skip = "CAUSAL_REVALIDATION_" + reason
            return None
        state._entry_causal_submit_context = {"side":str(side).upper(),"result":result}
        try:
            pos = await original_open(side, result, now)
        finally:
            state._entry_causal_submit_context = None
        if pos is None:
            pending = getattr(state, "mainnet_shadow_pending_entry", None)
            pending_id = _oid((pending or {}).get("result") or {})
            uncertain = bool(getattr(state, "execution_unknown", False) or getattr(state, "wstrade_execution_recovery_required", False))
            if not uncertain and pending_id != _oid(result):
                _release(shadow, result)
        return pos
    shadow._open_position = open_position
    original_advance = shadow._advance_shadow_pending
    async def advance(now):
        state = _state(shadow)
        before = getattr(state, "mainnet_shadow_pending_entry", None)
        before_result = ((before or {}).get("result") or {}) if before else None
        if before_result:
            side = str((before or {}).get("side") or before_result.get("side") or "").upper()
            ok, reason, detail = _validate(shadow, side, before_result, now, False)
            if not ok:
                state.mainnet_shadow_pending_entry = None
                _release(shadow, before_result)
                state.entry_causal_revalidation = {"stage":"SHADOW_MAKER_PENDING","ok":False,"reason":reason,"detail":detail,"checked_at":float(now)}
                return None
        out = await original_advance(now)
        if before_result and getattr(state, "mainnet_shadow_pending_entry", None) is None:
            pos = getattr(state, "mainnet_shadow_position", None)
            if pos is None or not bool(getattr(pos, "active", False)):
                _release(shadow, before_result)
        return out
    shadow._advance_shadow_pending = advance
    live = getattr(shadow, "live_execution", None)
    safety = getattr(live, "mainnet_safety", None) if live is not None else None
    original_gate = getattr(safety, "exchange_entry_gate", None)
    if callable(original_gate) and not getattr(safety, "_causal_gate_installed", False):
        async def gate(api, state, price, hard_sl, risk_plan=None):
            report = await original_gate(api, state, price, hard_sl, risk_plan=risk_plan)
            try: gate_ok, gate_reason, gate_detail = report
            except (TypeError, ValueError): return report
            if not gate_ok: return report
            ctx = getattr(state, "_entry_causal_submit_context", None)
            if not isinstance(ctx, dict): return report
            ok, reason, detail = _validate(shadow, ctx.get("side"), ctx.get("result"), time.time(), True)
            state.entry_causal_revalidation = {"stage":"POST_REST_PREFLIGHT","ok":ok,"reason":reason,"detail":detail,"checked_at":time.time()}
            if not ok:
                merged = dict(gate_detail or {})
                merged["causal_revalidation"] = {"reason":reason,"detail":detail}
                return False, "CAUSAL_REVALIDATION_" + reason, merged
            return report
        safety.exchange_entry_gate = gate
        safety._causal_gate_installed = True
    shadow._entry_causal_hardening_installed = True

def install(shadow, hardened=None):
    _patch_ignition(shadow)
    _patch_execution(shadow)
    state = _state(shadow)
    state.entry_causal_hardening = {
        "version":"ENTRY_CAUSAL_HARDENING_V1",
        "atr_max_age_seconds":ATR_MAX_AGE_SECONDS,
        "submit_max_age_seconds":SUBMIT_MAX_AGE_SECONDS,
        "bbo_max_age_seconds":BBO_MAX_AGE_SECONDS,
        "reserve_commit":True,"post_rest_validation":True,
        "futures_reversal_veto":True,"frozen_oi_timestamp":True,
    }
    return state.entry_causal_hardening
