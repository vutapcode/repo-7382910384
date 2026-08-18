"""Tier-S causal entry edge classifier with bounded regime/calibration adaptation."""
from loi_he_thong import entry_microstructure as micro
from loi_he_thong import microstructure_regime as regime_engine
from loi_he_thong import empirical_edge_calibrator as calibrator

VERSION = "ENTRY_EDGE_TIER_S_V6_ADAPTIVE"
FEE_ROUNDTRIP_BPS = 10.0
SLIPPAGE_BUFFER_BPS = 4.0
SAFETY_BUFFER_BPS = 4.0
TOTAL_COST_BPS = FEE_ROUNDTRIP_BPS + SLIPPAGE_BUFFER_BPS + SAFETY_BUFFER_BPS
NORMAL_MIN_COST_MULTIPLE = 1.35
FAST_MIN_COST_MULTIPLE = 1.75
EDGE_BPS = {
    "LOW_EDGE": 0.0, "NORMAL_EDGE": 35.0,
    "HIGH_EDGE": 55.0, "RUNNER_EDGE": 80.0,
}

def _entry_votes(result):
    return (result or {}).get("s_votes") or {}

def _price_metrics(result):
    return ((_entry_votes(result).get("S1_cross_venue_price_acceptance") or {}).get("metrics") or {})

def _flow_metrics(result):
    return ((_entry_votes(result).get("S2_multi_venue_executed_flow") or {}).get("metrics") or {})

def _bias_votes(state):
    return (getattr(state, "bias_council", None) or {}).get("s_votes") or {}

def _bias_aligned(state, key, side):
    vote = (_bias_votes(state).get(key) or {}).get("vote")
    return str(vote or "").upper() == str(side or "").upper()

def fast_contract_ok(result):
    if not result or result.get("decision") != "GO" or result.get("entry_mode") != "FAST":
        return False
    pm, fm = _price_metrics(result), _flow_metrics(result)
    return (
        len(pm.get("strong_supporters") or ()) == 3
        and len(fm.get("strong_supporters") or ()) >= 1
        and not (fm.get("strong_opponents") or ())
        and float(result.get("bias_confidence") or 0.0) >= 0.72
    )

def normal_contract_ok(result):
    if not result or result.get("decision") != "GO":
        return False
    votes = _entry_votes(result)
    s1 = votes.get("S1_cross_venue_price_acceptance") or {}
    s2 = votes.get("S2_multi_venue_executed_flow") or {}
    return s1.get("status") == "PASS" and s2.get("status") == "PASS"

def classify(result, state):
    side = str((result or {}).get("side") or getattr(state, "bias_state", "") or "").upper()
    mode = str((result or {}).get("entry_mode") or "NONE").upper()
    normal_ok, fast_ok = normal_contract_ok(result), fast_contract_ok(result)
    pm, fm = _price_metrics(result), _flow_metrics(result)
    price3 = len(pm.get("strong_supporters") or ()) == 3
    strong_flow = len(fm.get("strong_supporters") or ())
    strong_opp = len(fm.get("strong_opponents") or ())
    impact = micro.price_impact(result)
    basis = micro.spot_perp_basis(result)

    cross = _bias_aligned(state, "S1_cross_price", side)
    price_x_oi = _bias_aligned(state, "S2_price_x_oi", side)
    multi_flow = _bias_aligned(state, "S3_multi_flow", side)
    bias_support = int(cross) + int(price_x_oi) + int(multi_flow)

    # Causal hard vetoes stay absolute and are evaluated before any adaptation.
    if not (normal_ok or fast_ok) or impact["absorbed"] or basis["perp_expansion"]:
        edge_class = "LOW_EDGE"
    elif price3 and strong_flow >= 1 and price_x_oi and bias_support >= 2:
        edge_class = "RUNNER_EDGE"
    elif price_x_oi and (normal_ok or fast_ok):
        edge_class = "HIGH_EDGE"
    else:
        edge_class = "NORMAL_EDGE"

    regime = regime_engine.classify(state)
    calibration = calibrator.factor(state, mode, regime["regime"])
    expected_base = EDGE_BPS[edge_class]
    expected_bps = expected_base * regime["expectancy_factor"] * calibration["factor"]
    cost_budget = TOTAL_COST_BPS * regime["cost_factor"]
    multiple = expected_bps / cost_budget if cost_budget > 0 else 999.0
    minimum = FAST_MIN_COST_MULTIPLE if mode == "FAST" else NORMAL_MIN_COST_MULTIPLE
    cost_ok = edge_class != "LOW_EDGE" and multiple >= minimum

    state.tier_s_entry_regime = regime
    state.tier_s_entry_calibration = calibration
    return {
        "version": VERSION,
        "edge_class": edge_class,
        "expected_excursion_bps_base": expected_base,
        "expected_excursion_bps_model": round(expected_bps, 4),
        "cost_budget_bps_model": round(cost_budget, 4),
        "cost_multiple_model": round(multiple, 4),
        "min_cost_multiple": minimum,
        "cost_ok": cost_ok,
        "entry_mode": mode,
        "normal_contract_ok": normal_ok,
        "fast_contract_ok": fast_ok,
        "price_3venue_strong": price3,
        "strong_flow_venues": strong_flow,
        "strong_opposing_flow_venues": strong_opp,
        "bias_s_support": bias_support,
        "price_x_oi_aligned": price_x_oi,
        "cross_price_aligned": cross,
        "multi_flow_aligned": multi_flow,
        "price_impact": impact,
        "spot_perp_basis": basis,
        "micro_regime": regime,
        "empirical_calibration": calibration,
        "policy": "CAUSAL_VETO_FIRST_REGIME_AND_EMPIRICAL_EXPECTANCY_ONLY",
    }

def authorize(result, state):
    edge = classify(result, state)
    return bool((result or {}).get("decision") == "GO" and edge["cost_ok"]), edge
