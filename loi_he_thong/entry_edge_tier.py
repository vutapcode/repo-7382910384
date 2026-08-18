"""Tier-S entry edge classifier.

Purpose:
- preserve recall: OI never becomes a hard timing prerequisite;
- raise post-cost expectancy by classifying GO setups as NORMAL/HIGH/RUNNER;
- allow strict GO_FAST only when its own causal contract is satisfied.
"""
from loi_he_thong import entry_microstructure as micro

VERSION = "ENTRY_EDGE_TIER_S_V3_BASIS"

FEE_ROUNDTRIP_BPS = 10.0
SLIPPAGE_BUFFER_BPS = 4.0
SAFETY_BUFFER_BPS = 4.0
TOTAL_COST_BPS = FEE_ROUNDTRIP_BPS + SLIPPAGE_BUFFER_BPS + SAFETY_BUFFER_BPS

NORMAL_MIN_COST_MULTIPLE = 1.35
FAST_MIN_COST_MULTIPLE = 1.75

EDGE_BPS = {
    "LOW_EDGE": 0.0,
    "NORMAL_EDGE": 35.0,
    "HIGH_EDGE": 55.0,
    "RUNNER_EDGE": 80.0,
}


def _entry_votes(result):
    return (result or {}).get("s_votes") or {}


def _entry_price_metrics(result):
    votes = _entry_votes(result)
    seat = votes.get("S1_cross_venue_price_acceptance") or {}
    return seat.get("metrics") or {}


def _entry_flow_metrics(result):
    votes = _entry_votes(result)
    seat = votes.get("S2_multi_venue_executed_flow") or {}
    return seat.get("metrics") or {}


def _bias_votes(state):
    council = getattr(state, "bias_council", None) or {}
    return council.get("s_votes") or {}


def _bias_aligned(state, key, side):
    vote = (_bias_votes(state).get(key) or {}).get("vote")
    return str(vote or "").upper() == str(side or "").upper()


def fast_contract_ok(result):
    """GO_FAST may reduce material-flow venue count, never evidence quality."""
    if not result or result.get("decision") != "GO" or result.get("entry_mode") != "FAST":
        return False
    pm = _entry_price_metrics(result)
    fm = _entry_flow_metrics(result)
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
    """Classify expected excursion without turning slow OI into an entry veto."""
    side = str((result or {}).get("side") or getattr(state, "bias_state", "") or "").upper()
    mode = str((result or {}).get("entry_mode") or "NONE").upper()
    normal_ok = normal_contract_ok(result)
    fast_ok = fast_contract_ok(result)

    pm = _entry_price_metrics(result)
    fm = _entry_flow_metrics(result)
    price3 = len(pm.get("strong_supporters") or ()) == 3
    strong_flow = len(fm.get("strong_supporters") or ())
    strong_opp = len(fm.get("strong_opponents") or ())
    impact = micro.price_impact(result)
    basis = micro.spot_perp_basis(result)

    cross = _bias_aligned(state, "S1_cross_price", side)
    price_x_oi = _bias_aligned(state, "S2_price_x_oi", side)
    multi_flow = _bias_aligned(state, "S3_multi_flow", side)
    bias_s_support = int(cross) + int(price_x_oi) + int(multi_flow)

    if not (normal_ok or fast_ok) or impact["absorbed"] or basis["perp_expansion"]:
        edge_class = "LOW_EDGE"
    elif price3 and strong_flow >= 1 and price_x_oi and bias_s_support >= 2:
        edge_class = "RUNNER_EDGE"
    elif price_x_oi and (normal_ok or fast_ok):
        edge_class = "HIGH_EDGE"
    else:
        edge_class = "NORMAL_EDGE"

    expected_bps = EDGE_BPS[edge_class]
    cost_multiple = expected_bps / TOTAL_COST_BPS if TOTAL_COST_BPS > 0 else 999.0
    min_multiple = FAST_MIN_COST_MULTIPLE if mode == "FAST" else NORMAL_MIN_COST_MULTIPLE
    cost_ok = edge_class != "LOW_EDGE" and cost_multiple >= min_multiple

    return {
        "version": VERSION,
        "edge_class": edge_class,
        "expected_excursion_bps_model": expected_bps,
        "cost_budget_bps_model": TOTAL_COST_BPS,
        "cost_multiple_model": round(cost_multiple, 4),
        "min_cost_multiple": min_multiple,
        "cost_ok": cost_ok,
        "entry_mode": mode,
        "normal_contract_ok": normal_ok,
        "fast_contract_ok": fast_ok,
        "price_3venue_strong": price3,
        "strong_flow_venues": strong_flow,
        "strong_opposing_flow_venues": strong_opp,
        "bias_s_support": bias_s_support,
        "price_x_oi_aligned": price_x_oi,
        "cross_price_aligned": cross,
       "multi_flow_aligned": multi_flow,
        "price_impact": impact,
        "spot_perp_basis": basis,
        "policy": "OI_UPGRADES_EXPECTANCY_NEVER_TIMING_VETO",
    }


def authorize(result, state):
    edge = classify(result, state)
    allowed = bool((result or {}).get("decision") == "GO" and edge["cost_ok"])
    return allowed, edge
