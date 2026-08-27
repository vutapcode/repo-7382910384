"""Seven-question causal audit before Residual Edge authorization.

This module does not create direction, estimate alpha, or read recorder output.
It consolidates evidence already produced by Bias and Ignition so a forced
unwind tail cannot look like fresh whale commitment merely because sell/buy
market orders are large.
"""

VERSION = "ENTRY_THESIS_GATE_V4_FLOW_SURVIVAL"
CASH = frozenset(("binance_spot", "coinbase_spot"))
BIAS_MIN_CONF = 0.55
MAX_CONSUMED = 0.35
FLOW_IMBALANCE = 0.20
MATERIAL_PRICE_BPS = 0.15


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bias_question(result, ignition):
    side = str((result or {}).get("side") or "ABSTAIN").upper()
    frozen = dict(ignition.get("bias_snapshot") or {})
    context = dict(frozen.get("direction_context") or {})
    direction = str(frozen.get("direction") or side).upper()
    confidence = _f(frozen.get("confidence"), _f((result or {}).get("bias_confidence")))
    phase = str(context.get("phase") or "UNKNOWN").upper()
    candidate_side = str(context.get("candidate_side") or "ABSTAIN").upper()
    conflict = bool(
        direction not in (side, "ABSTAIN")
        or (phase == "REVERSAL_CANDIDATE" and candidate_side not in (side, "ABSTAIN"))
    )
    verified = bool(frozen)
    passed = bool(not conflict and (not verified or confidence >= BIAS_MIN_CONF))
    return {
        "question": "BACKGROUND_DIRECTION_REAL",
        "status": "PASS" if passed else "FAIL",
        "frozen_snapshot": verified, "direction": direction,
        "confidence": round(confidence, 6), "phase": phase,
        "candidate_side": candidate_side, "conflict": conflict,
        "authority": "FROZEN_PRE_IMPULSE_BIAS",
    }


def _intent_question(ignition, liquidation):
    oi = dict(ignition.get("oi_intent") or {})
    verification = dict(ignition.get("oi_verification_state") or {})
    verification_status = str(
        verification.get("status") or "UNAVAILABLE"
    ).upper()
    intent = str(
        verification.get("intent") or oi.get("intent") or "NEUTRAL"
    ).upper()
    fresh = verification_status.startswith("FRESH_")
    phase = str((liquidation or {}).get("phase") or "UNKNOWN").upper()
    forced = bool(
        fresh and intent == "UNWIND"
        or (liquidation or {}).get("burst")
        or (liquidation or {}).get("decelerating")
    )
    classification = (
        "POSITION_BUILD" if fresh and intent == "POSITION_BUILD" else
        "LIQUIDATION_TAIL" if (liquidation or {}).get("decelerating") else
        "LIQUIDATION_CASCADE" if (liquidation or {}).get("burst") else
        "UNWIND" if fresh and intent == "UNWIND" else
        "NEUTRAL_OR_UNVERIFIED"
    )
    return {
        "question": "NEW_MONEY_OR_FORCED_UNWIND",
        "status": classification, "oi_intent": intent,
        "oi_fresh": fresh, "oi_verification_status": verification_status,
        "oi_causal_class": oi.get("causal_class"),
        "force_order_phase": phase, "forced_closing_risk": forced,
    }


def _flow_question(result, ignition, impact):
    flow = dict(ignition.get("flow_by_venue") or {})
    cash = set(ignition.get("cash_venues") or ()) & CASH
    rows = [dict(flow.get(name) or {}) for name in cash if flow.get(name)]
    recent_imbalances = [
        _f(row.get("recent_1s_signed_imbalance"), _f(row.get("signed_imbalance")))
        for row in rows
    ]
    recent_progress = []
    for row in rows:
        if row.get("recent_1s_price_progress_bps") is not None:
            recent_progress.append(_f(row.get("recent_1s_price_progress_bps")))
        elif row.get("price_conversion_bps") is not None:
            recent_progress.append(_f(row.get("price_conversion_bps")))
    moves = dict(ignition.get("venue_moves_bps") or {})
    cash_move = max((_f(moves.get(name)) for name in cash), default=0.0)
    flow_strength = (
        sum(max(0.0, value) for value in recent_imbalances)
        / len(recent_imbalances) if recent_imbalances else 0.0
    )
    recent_cash_progress = max(recent_progress) if recent_progress else cash_move
    threshold = max(
        MATERIAL_PRICE_BPS, _f((result or {}).get("price_threshold_bps"))
    )
    strong_flow = bool(flow_strength >= FLOW_IMBALANCE)
    legacy_absorption = bool(
        (impact or {}).get("absorbed")
        or (
            strong_flow and rows
            and recent_cash_progress < threshold * 0.70
        )
    )
    efficiency = dict(ignition.get("flow_efficiency") or {})
    venue_efficiency = dict(efficiency.get("venues") or {})
    proposer = str(ignition.get("proposer") or "").lower()
    primary = proposer if proposer in CASH else (
        sorted(cash)[0] if cash else None
    )
    primary_state = str(
        (venue_efficiency.get(primary) or {}).get("state") or "UNKNOWN"
    ).upper()
    primary_efficiency = dict(venue_efficiency.get(primary) or {})
    marginal_now = primary_efficiency.get("marginal_conversion_now_bps")
    marginal_previous = primary_efficiency.get("previous_conversion_bps")
    other_states = {
        name: str((venue_efficiency.get(name) or {}).get("state") or "UNKNOWN").upper()
        for name in cash if name != primary
    }
    independent_continuation = any(
        state == "CONTINUING_CONFIRMED" for state in other_states.values()
    )
    composite_veto = bool(
        primary_state in ("ABSORBED", "EXHAUSTED")
        and not independent_continuation
    )
    # Episode progress is maturity diagnostics only. It must never authorize
    # present-tense conversion after the executed-flow window went quiet.
    converts = bool(
        not composite_veto
        and (
            primary_state == "CONTINUING_CONFIRMED"
            or independent_continuation
        )
    )
    if composite_veto:
        status = primary_state
    elif primary_state == "CONTINUING_CONFIRMED" or independent_continuation:
        status = "CONTINUING_CONFIRMED"
    elif (
        primary_state == "REACCELERATION_UNCONFIRMED"
        or "REACCELERATION_UNCONFIRMED" in other_states.values()
    ):
        status = "REACCELERATION_UNCONFIRMED"
    elif primary_state == "FADING" or "FADING" in other_states.values():
        status = "FADING"
    elif primary_state == "DECAYING" or "DECAYING" in other_states.values():
        status = "DECAYING"
    else:
        status = "UNKNOWN"
    return {
        "question": "EXECUTED_FLOW_CONVERTS_TO_PRICE",
        "status": status,
        "cash_flow_strength": round(flow_strength, 6),
        "recent_cash_progress_bps": round(recent_cash_progress, 6),
        "episode_cash_progress_bps": round(cash_move, 6),
        "marginal_conversion_now_bps": marginal_now,
        "marginal_conversion_previous_bps": marginal_previous,
        "material_price_bps": round(threshold, 6),
        "price_impact": dict(impact or {}),
        "legacy_absorption_observed": legacy_absorption,
        "primary_cash_anchor": primary,
        "primary_state": primary_state,
        "other_cash_states": other_states,
        "independent_cash_continuation": independent_continuation,
        "composite_veto": composite_veto,
        "converts": converts,
        "flow_efficiency": efficiency,
        "policy": "MARGINAL_EXECUTED_FLOW_CONVERSION_NO_EPISODE_PROGRESS_AUTHORITY",
    }


def _liquidity_question(flow_question):
    # The only full depth analyzer runs in the separate recorder process and
    # is authority=false. Never read a stale file or invent an IPC snapshot.
    absorbed = flow_question.get("status") in ("ABSORBED", "EXHAUSTED")
    return {
        "question": "LIQUIDITY_ACCEPTS_OR_ABSORBS",
        "status": "PRICE_FLOW_ABSORPTION" if absorbed else "UNAVAILABLE",
        "liquidity_response": "RECORDER_RESEARCH_ONLY",
        "depth_authority": False,
        "executed_flow_price_proxy": bool(absorbed),
        "policy": "NO_STATIC_WALL_OR_CANCEL_AUTHORITY",
    }


def _maturity_question(ignition):
    phase = dict(ignition.get("phase_measurement") or {})
    reported = _f(ignition.get("consumed_fraction"), 1.0)
    scale = _f(phase.get("phase_scale_bps"))
    shared_progress = max(
        _f(phase.get("cash_displacement_bps")),
        _f(phase.get("episode_cash_displacement_bps")),
        _f(phase.get("precursor_cash_displacement_bps")),
    )
    reconstructed = shared_progress / scale if scale > 0.0 else reported
    shared = max(reported, reconstructed)
    reset_mismatch = bool(reconstructed > reported + 1e-6)
    return {
        "question": "SHARED_WAVE_EARLY_OR_MATURE",
        "status": "MATURE" if shared >= MAX_CONSUMED else "EARLY",
        "reported_consumed_fraction": round(reported, 6),
        "reconstructed_consumed_fraction": round(reconstructed, 6),
        "shared_wave_consumed": round(shared, 6),
        "consumed_reset_mismatch": reset_mismatch,
        "phase_source": phase.get("source"),
    }


def _independence_question(ignition, basis):
    cash = set(ignition.get("cash_venues") or ()) & CASH
    proposer = str(ignition.get("proposer") or "UNKNOWN").lower()
    futures_self_led = bool(
        proposer == "futures" and not ignition.get("futures_cash_response_ok")
    )
    status = (
        "DERIVATIVES_LED_REJECT" if futures_self_led or (basis or {}).get("perp_expansion") else
        "DUAL_INDEPENDENT_CASH" if cash == CASH else
        "SINGLE_CASH_ANCHOR" if cash else "NO_CASH_AUTHORITY"
    )
    return {
        "question": "EXCHANGE_INDEPENDENCE",
        "status": status, "cash_venues": sorted(cash),
        "proposer": proposer, "futures_self_led": futures_self_led,
        "spot_perp_basis": dict(basis or {}),
    }


def evaluate(state, result, impact, basis, liquidation):
    ignition = dict((result or {}).get("ignition") or {})
    q1 = _bias_question(result, ignition)
    q2 = _intent_question(ignition, liquidation)
    q3 = _flow_question(result, ignition, impact)
    q4 = _liquidity_question(q3)
    q5 = _maturity_question(ignition)
    q6 = _independence_question(ignition, basis)
    dual_cash = q6["status"] == "DUAL_INDEPENDENT_CASH"
    forced = bool(q2["forced_closing_risk"])
    exhausted = q3["status"] in ("ABSORBED", "EXHAUSTED")
    mature = q5["status"] == "MATURE"
    liquidation_tail = q2["status"] == "LIQUIDATION_TAIL"
    persistent = str((result or {}).get("entry_mode") or "").upper() == (
        "PERSISTENT_METAORDER"
    )

    # Preserve good cash-led unwind opportunities. Forced closure becomes a
    # veto only when at least one independent symptom says the wave is ending:
    # price non-conversion, mature shared wave, or a decelerating forceOrder
    # tail. A single-cash unwind needs stronger protection than dual cash.
    forced_tail_veto = bool(
        forced and (
            liquidation_tail
            or exhausted
            or q3.get("legacy_absorption_observed")
            or (mature and not dual_cash)
        )
    )
    blockers = []
    soft_waits = []
    if q1["status"] == "FAIL":
        blockers.append("BIAS_THESIS_FAIL")
    if q6["status"] in ("DERIVATIVES_LED_REJECT", "NO_CASH_AUTHORITY"):
        blockers.append("EXCHANGE_INDEPENDENCE_FAIL")
    replay_approved = bool(
        getattr(state, "entry_economics_v3_replay_approved", False)
    )
    if replay_approved and q3.get("composite_veto"):
        blockers.append("FLOW_EFFICIENCY_V3_VETO")
    if forced_tail_veto:
        blockers.append("UNWIND_TAIL_VETO")
    # Persistence proves that a causal wave existed; it does not prove that a
    # taker entry is still timely now.  DECAYING/UNKNOWN may recover on a later
    # executed-flow window, so keep the episode retryable instead of turning
    # either state into a hard veto.
    if persistent and q3["status"] in ("DECAYING", "UNKNOWN"):
        soft_waits.append("WAIT_PERSISTENT_FLOW_EFFICIENCY")
    elif persistent and q3["status"] == "FADING":
        soft_waits.append("WAIT_PERSISTENT_FLOW_FADING")
    elif persistent and q3["status"] == "REACCELERATION_UNCONFIRMED":
        soft_waits.append("WAIT_PERSISTENT_REACCELERATION_CONFIRMATION")
    return {
        "version": VERSION,
        "decision": "WAIT" if blockers or soft_waits else "PASS",
        "questions": {
            "q1_bias": q1, "q2_intent": q2, "q3_flow_efficiency": q3,
            "q4_liquidity": q4, "q5_maturity": q5,
            "q6_exchange_independence": q6,
        },
        "blocking_reasons": blockers,
        "soft_wait_reasons": soft_waits,
        "forced_unwind_tail": forced_tail_veto,
        "entry_economics_v3_replay_approved": replay_approved,
        "policy": "COMPOSITE_CAUSAL_VETO_NO_SINGLE_SIGNAL_DIRECTION",
    }


def attach_economics(report, *, total_cost_bps, reserve_bps, economic_ok,
                     forward_edge=None):
    output = dict(report or {})
    questions = dict(output.get("questions") or {})
    questions["q7_economics"] = {
        "question": "NET_EDGE_AFTER_EXECUTABLE_COST",
        "status": "PASS" if economic_ok else "BOOTSTRAP_OR_FAIL",
        "total_cost_bps": round(_f(total_cost_bps), 6),
        "minimum_net_reserve_bps": round(_f(reserve_bps), 6),
        "authority": "RESIDUAL_EDGE_EMPIRICAL_COHORT",
        "forward_edge": dict(forward_edge or {}),
        "cost_counted_once": True,
    }
    output["questions"] = questions
    return output
