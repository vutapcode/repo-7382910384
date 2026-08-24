"""Residual Edge gate for Ignition Core V1.

The old 13/20/35 bps range prior is retained only as historical metadata. It
has no authorization power. Shadow may collect structurally valid bootstrap
trades; real money additionally requires persisted empirical expectancy/LCB.
"""

from loi_he_thong import edge_calibration_v2
from loi_he_thong import entry_microstructure as micro
from loi_he_thong import microstructure_regime as regime_engine
from loi_he_thong import verified_cost_model

VERSION = "IGNITION_RESIDUAL_EDGE_V1"
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
    return bool(
        (result or {}).get("decision") == "GO"
        and ignition.get("state") == "PROVE"
        and proof in ("METAORDER_CONTINUATION", "FAILED_REVERSION")
        and ignition.get("cash_venues")
        and _f(ignition.get("consumed_fraction"), 1.0) <= 0.35
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
    # A qualified Ignition bucket already requires cash price conversion. Keep
    # old microstructure output as metadata, but never infer absorption merely
    # because compatibility vote names differ from the retired Council.
    if impact.get("absorbed") and max(cash_moves or [0.0]) >= 0.15:
        impact = dict(impact, status="PASS", absorbed=False, efficient=True)
    hard_vetoes = []
    if candidate and not contract_ok:
        hard_vetoes.append("IGNITION_CONTRACT_FAIL")
    if impact.get("absorbed"):
        hard_vetoes.append("ABSORPTION_VETO")
    if basis.get("perp_expansion"):
        hard_vetoes.append("PERP_LED_VETO")

    regime = regime_engine.classify(state, side)
    costs = verified_cost_model.estimate(result, state)
    residual = max(0.0, _f(ignition.get("residual_edge_proxy_bps")))
    cost_budget = max(0.0, _f(costs.get("total_cost_bps")))
    reserve = max(0.0, _f(costs.get("minimum_net_edge_bps")))
    expected_net = residual - cost_budget
    economic_ok = bool(not hard_vetoes and expected_net >= reserve)

    if not candidate:
        edge_class = "NOT_CANDIDATE"
    elif hard_vetoes:
        edge_class = "HARD_VETO"
    elif economic_ok:
        edge_class = "RESIDUAL_POSITIVE"
    else:
        edge_class = "BOOTSTRAP_UNVERIFIED"

    calibration = edge_calibration_v2.factor(
        state, mode, str(regime.get("regime") or "NORMAL"), side, edge_class,
        ignition.get("proof_type"), ignition.get("proposer"),
        costs.get("execution_style"),
    )
    samples = int(calibration.get("samples", 0) or 0)
    empirical_mean = calibration.get("mean_net_bps")
    empirical_lcb = calibration.get("lower_confidence_bound_bps")
    promotion = getattr(state, "wstrade_promotion", None) or {}
    promotion_trades = int(promotion.get("shadow_trades", 0) or 0)
    stress_total = _f(promotion.get("stress_25bps_pnl_usdt"), -1.0)
    # PromotionController reports version-bound deltas. Lifetime shadow totals
    # contain outcomes from retired Entry versions and must not contaminate the
    # Ignition cohort.
    stress_ok = bool(promotion_trades >= 30 and stress_total >= 0.0)
    live_empirical_ok = bool(
        samples >= 30
        and calibration.get("live_empirical_ok")
        and empirical_mean is not None and _f(empirical_mean) > 0.0
        and empirical_lcb is not None and _f(empirical_lcb) >= 0.0
        and stress_ok and not hard_vetoes
        and costs.get("commission_verified")
    )
    live = bool(getattr(state, "wstrade_live_armed", False))
    bootstrap_shadow_allowed = bool(contract_ok and not hard_vetoes and not live)

    state.tier_s_entry_regime = regime
    state.tier_s_entry_calibration = calibration
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
        "commission_verified": bool(costs.get("commission_verified")),
        "commission_source": costs.get("commission_source"),
        "cost_components": costs, "normal_contract_ok": contract_ok,
        "fast_contract_ok": False, "hard_vetoes": hard_vetoes,
        "price_impact": impact, "spot_perp_basis": basis,
        "micro_regime": regime, "empirical_calibration": calibration,
        "empirical_alpha": {
            "samples": samples, "mean_net_bps": empirical_mean,
            "lower_confidence_bound_bps": empirical_lcb,
            "stress_25bps_ok": stress_ok,
            "commission_verified": bool(costs.get("commission_verified")),
            "status": calibration.get("status"),
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
