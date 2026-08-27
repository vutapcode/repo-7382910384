"""Residual Edge gate for Ignition Core V1.

The old 13/20/35 bps range prior is retained only as historical metadata. It
has no authorization power. Shadow may collect structurally valid bootstrap
trades; real money additionally requires persisted empirical expectancy/LCB.
"""

import time

from loi_he_thong import edge_calibration_v2
from loi_he_thong import entry_economics_v2
from loi_he_thong import entry_thesis_gate
from loi_he_thong import entry_microstructure as micro
from loi_he_thong import liquidation_context
from loi_he_thong import microstructure_regime as regime_engine
from loi_he_thong import verified_cost_model

VERSION = "IGNITION_ENTRY_ECONOMICS_V3"
EDGE_BPS = {
    "LOW_EDGE": 0.0, "NORMAL_EDGE": 13.0,
    "HIGH_EDGE": 20.0, "RUNNER_EDGE": 35.0,
}


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ignition_contract(result):
    ignition = (result or {}).get("ignition") or {}
    proposer = str(ignition.get("proposer") or "")
    proof = str(ignition.get("proof_type") or "")
    persistent = str((result or {}).get("entry_mode") or "").upper() == "PERSISTENT_METAORDER"
    accepted_proofs = (
        ("PERSISTENT_METAORDER",) if persistent
        else ("METAORDER_CONTINUATION", "FAILED_REVERSION")
    )
    return bool(
        (result or {}).get("decision") == "GO"
        and ignition.get("state") == "PROVE"
        and proof in accepted_proofs
        and ignition.get("cash_venues")
        and _f(ignition.get("consumed_fraction"), 1.0) <= 0.35
        and (not persistent or proposer in ("binance_spot", "coinbase_spot"))
        and (proposer != "futures" or ignition.get("futures_cash_response_ok"))
        and (proposer == "futures" or ignition.get("futures_follow_ok"))
    )


def fast_contract_ok(result):
    return False


def normal_contract_ok(result):
    return _ignition_contract(result)


def classify(result, state):
    ignition = (result or {}).get("ignition") or {}
    side = str((result or {}).get("side") or getattr(state, "bias_state", "") or "").upper()
    mode = str((result or {}).get("entry_mode") or "IGNITION").upper()
    candidate = bool((result or {}).get("decision") == "GO")
    contract_ok = _ignition_contract(result)
    impact = micro.price_impact(result)
    basis = micro.spot_perp_basis(result)
    moves = ignition.get("venue_moves_bps") or {}
    cash_moves = [_f(moves.get(name)) for name in ("binance_spot", "coinbase_spot") if name in moves]
    futures_move = _f(moves.get("futures"))
    cash_best = max(cash_moves) if cash_moves else 0.0
    perp_lead = futures_move - cash_best
    if perp_lead > 3.0:
        basis = dict(basis, status="PERP_EXPANSION", perp_expansion=True,
                     lead_bps=round(perp_lead, 6), limit_bps=3.0)
    hard_vetoes = []
    if candidate and not contract_ok:
        hard_vetoes.append("IGNITION_CONTRACT_FAIL")
    v3_replay_approved = bool(
        getattr(state, "entry_economics_v3_replay_approved", False)
    )
    # Preserve baseline demo semantics until a canonical replay explicitly
    # approves V3. Once approved, the persistent cross-cash classifier replaces
    # this single-snapshot legacy veto instead of stacking both vetoes.
    if candidate and not v3_replay_approved and bool(impact.get("absorbed")):
        hard_vetoes.append("ABSORPTION_VETO")
    if basis.get("perp_expansion"):
        hard_vetoes.append("PERP_LED_VETO")

    liquidation = liquidation_context.assess_entry(
        state, result, _f((result or {}).get("ts"), time.time())
    )
    live = bool(getattr(state, "wstrade_live_armed", False))
    thesis_audit = entry_thesis_gate.evaluate(
        state, result, impact, basis, liquidation
    )
    soft_waits = list(thesis_audit.get("soft_wait_reasons") or ())
    if candidate:
        hard_vetoes.extend(thesis_audit.get("blocking_reasons") or ())

    regime = regime_engine.classify(state, side)
    costs = verified_cost_model.estimate(result, state)
    cost_contract = verified_cost_model.freeze_execution_cost_contract(
        result, state
    )
    residual = max(0.0, _f(ignition.get("residual_edge_proxy_bps")))
    cost_budget = max(0.0, _f(costs.get("total_cost_bps")))
    reserve = max(0.0, _f(costs.get("minimum_net_edge_bps")))
    economic_snapshot = entry_economics_v2.feature_snapshot(
        result, regime, costs.get("execution_style"), thesis_audit,
    )
    forward_edge = entry_economics_v2.estimate(state, economic_snapshot)
    if (
        candidate and forward_edge.get("status") == "ACTIVE"
        and not forward_edge.get("positive_net")
    ):
        hard_vetoes.append("EMPIRICAL_FORWARD_EDGE_FAIL")
    expected_net = residual - cost_budget
    empirical_forward_ok = bool(
        forward_edge.get("status") == "ACTIVE"
        and forward_edge.get("positive_net")
    )
    economic_ok = bool(
        not hard_vetoes and not soft_waits
        and (empirical_forward_ok or expected_net >= reserve)
    )
    thesis_audit = entry_thesis_gate.attach_economics(
        thesis_audit, total_cost_bps=cost_budget,
        reserve_bps=reserve, economic_ok=economic_ok,
        forward_edge=forward_edge,
    )

    if not candidate:
        edge_class = "NOT_CANDIDATE"
    elif soft_waits:
        edge_class = "WAIT_EVIDENCE"
    elif hard_vetoes:
        edge_class = "HARD_VETO"
    elif economic_ok:
        edge_class = "RESIDUAL_POSITIVE"
    else:
        edge_class = "BOOTSTRAP_UNVERIFIED"

    calibration = edge_calibration_v2.factor(
        state, mode, str(regime.get("regime") or "NORMAL"), side, edge_class,
        ignition.get("proof_type"), ignition.get("proposer"),
        costs.get("execution_style"), current_cost_bps=cost_budget,
        minimum_net_edge_bps=reserve,
    )
    samples = int(calibration.get("samples", 0) or 0)
    execution_cost_samples = int(
        calibration.get("execution_cost_samples", 0) or 0
    )
    empirical_mean = calibration.get("mean_net_bps")
    empirical_lcb = calibration.get("lower_confidence_bound_bps")
    current_cost_adjustment = dict(calibration.get("current_cost_adjustment") or {})
    raw_empirical_ok = bool(
        empirical_mean is not None and _f(empirical_mean) > 0.0
        and empirical_lcb is not None and _f(empirical_lcb) >= 0.0
    )
    adjusted_pass_raw_fail = bool(
        samples >= 30
        and str(calibration.get("level") or "").upper() == "EXACT"
        and current_cost_adjustment.get("live_empirical_ok")
        and not raw_empirical_ok
    )
    cost_gate_telemetry = {
        "status": "ADJUSTED_PASS_RAW_FAIL" if adjusted_pass_raw_fail else "NO_CONFLICT",
        "authority": False,
        "exact_cohort_key": calibration.get("bucket"),
        "samples": samples,
        "historical_mean_net_bps": empirical_mean,
        "historical_lower_confidence_bound_bps": empirical_lcb,
        "adjusted_mean_net_bps": current_cost_adjustment.get("mean_net_bps"),
        "adjusted_lower_confidence_bound_bps": current_cost_adjustment.get(
            "lower_confidence_bound_bps"
        ),
        "current_execution_cost_bps": round(cost_budget, 6),
        "minimum_net_edge_bps": round(reserve, 6),
        "side": side,
        "proof_type": ignition.get("proof_type"),
        "proposer": ignition.get("proposer"),
        "regime": str(regime.get("regime") or "NORMAL"),
        "execution_style": costs.get("execution_style"),
    }
    promotion = getattr(state, "wstrade_promotion", None) or {}
    promotion_trades = int(promotion.get("shadow_trades", 0) or 0)
    stress_total = _f(promotion.get("stress_25bps_pnl_usdt"), -1.0)
    # PromotionController reports version-bound deltas. Lifetime shadow totals
    # contain outcomes from retired Entry versions and must not contaminate the
    # Ignition cohort.
    stress_ok = bool(promotion_trades >= 30 and stress_total >= 0.0)
    live_empirical_ok = bool(
        samples >= 30
        and str(calibration.get("level") or "").upper() == "EXACT"
        and calibration.get("live_empirical_ok")
        and raw_empirical_ok
        and stress_ok and not hard_vetoes
        and costs.get("commission_verified")
        and forward_edge.get("status") == "ACTIVE"
        and forward_edge.get("level") == "EXACT"
        and forward_edge.get("positive_net")
        and not soft_waits
    )
    bootstrap_shadow_allowed = bool(
        contract_ok and not hard_vetoes and not soft_waits and not live
    )
    research_probe_allowed = bool(bootstrap_shadow_allowed)
    ledger_type = "LIVE_LIKE_SHADOW" if live_empirical_ok else "RESEARCH_PROBE"
    execution_urgency = {
        "status": "EXECUTION_URGENCY_UNVERIFIED",
        "authority": False,
        "selected_style": costs.get("execution_style"),
        "alternative_outcomes_required": (
            "TAKER_NOW_VS_MAKER_IF_EXECUTABLE_VS_SHORT_DELAY"
        ),
        "reason": "NO_MATCHED_EMPIRICAL_ALTERNATIVE_COHORT",
    }

    state.tier_s_entry_regime = regime
    state.tier_s_entry_calibration = calibration
    state.tier_s_liquidation_context = liquidation
    state.tier_s_entry_thesis_audit = thesis_audit
    return {
        "version": VERSION, "edge_class": edge_class,
        "entry_mode": mode, "execution_style": costs.get("execution_style"),
        "residual_edge_proxy_bps": round(residual, 6),
        "expected_excursion_bps_base": None,
        "expected_excursion_bps_prior": dict(EDGE_BPS),
        "expected_excursion_bps_model": round(residual, 6),
        "cost_budget_bps_model": round(cost_budget, 6),
        "expected_net_bps_model": round(expected_net, 6),
        "min_net_edge_bps": round(reserve, 6),
        "cost_multiple_model": round(residual / cost_budget, 6) if cost_budget > 0.0 else 999.0,
        "cost_ok": economic_ok, "bootstrap_shadow_allowed": bootstrap_shadow_allowed,
        "research_probe_allowed": research_probe_allowed,
        "live_like_shadow_allowed": live_empirical_ok,
        "shadow_ledger_type": ledger_type,
        "commission_verified": bool(costs.get("commission_verified")),
        "commission_source": costs.get("commission_source"),
        "current_execution_cost_bps": round(cost_budget, 6),
        "execution_cost_samples": execution_cost_samples,
        "execution_cost_distribution_bps": calibration.get(
            "execution_cost_distribution_bps"
        ),
        "execution_cost_authority": bool(
            calibration.get("execution_cost_authority")
        ),
        "execution_cost_contract": cost_contract,
        "economic_contract_version": entry_economics_v2.CONTRACT_VERSION,
        "entry_economics_v3_replay_approved": v3_replay_approved,
        "economic_feature_snapshot": economic_snapshot,
        "forward_edge_status": forward_edge.get("status"),
        "forward_edge": forward_edge,
        "time_to_edge_status": (
            "ACTIVE" if forward_edge.get("time_to_positive_net_p80_seconds") is not None
            else "BOOTSTRAP_UNVERIFIED"
        ),
        "time_to_edge": {
            "p80_seconds": forward_edge.get("time_to_positive_net_p80_seconds"),
            "winner_samples": forward_edge.get("time_to_positive_net_winners", 0),
            "authority": bool(
                forward_edge.get("authority")
                and forward_edge.get("time_to_positive_net_p80_seconds") is not None
            ),
            "policy": "DETERIORATION_EVIDENCE_NEVER_HARD_TIMEOUT",
        },
        "execution_urgency": execution_urgency,
        "cost_components": costs, "normal_contract_ok": contract_ok,
        "fast_contract_ok": False, "hard_vetoes": hard_vetoes,
        "soft_wait_reasons": soft_waits,
        "price_impact": impact, "spot_perp_basis": basis,
        "liquidation_context": liquidation,
        "entry_thesis_audit": thesis_audit,
        "micro_regime": regime, "empirical_calibration": calibration,
        "empirical_alpha": {
            "samples": samples, "mean_net_bps": empirical_mean,
            "lower_confidence_bound_bps": empirical_lcb,
            "stress_25bps_ok": stress_ok,
            "commission_verified": bool(costs.get("commission_verified")),
            "status": calibration.get("status"),
            "cost_gate_telemetry": cost_gate_telemetry,
        },
        "live_empirical_ok": live_empirical_ok,
        "policy": "SHADOW_BOOTSTRAP_LIVE_EMPIRICAL_GUARDIAN_OUTCOME_LCB",
    }


def authorize(result, state):
    report = classify(result, state)
    if bool(getattr(state, "wstrade_live_armed", False)):
        # Completed shadow outcomes are already net of executable fills, fees
        # and slippage.  Requiring the structural proxy again would recreate
        # the cash/Futures convergence contradiction fixed above.
        allowed = bool(report["live_empirical_ok"])
    else:
        allowed = bool(report["cost_ok"] or report["bootstrap_shadow_allowed"])
    return bool((result or {}).get("decision") == "GO" and allowed), report
