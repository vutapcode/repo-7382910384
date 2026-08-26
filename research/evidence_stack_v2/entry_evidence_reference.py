"""Research-only Entry evidence contract for Codex. AUTHORITY=False."""

AUTHORITY = False
VERSION = "ENTRY_EVIDENCE_V2_REFERENCE_V1"

ORDER = (
    "bias",
    "shared_wave_maturity",
    "cash_acceptance",
    "flow_conversion",
    "flow_efficiency",
    "oi_state",
    "liquidation_phase",
    "liquidity_response",
    "exchange_independence",
    "perp_basis",
    "economics",
)

def shared_wave_consumed(previous_max, current):
    """Same causal wave may mature, never become young again."""
    return max(max(0.0, float(previous_max)), max(0.0, float(current)))

def entry_evidence_label(
    *,
    bias_aligned,
    wave_mature,
    cash,
    conversion,
    efficiency,
    oi,
    liquidation,
    liquidity,
    independence,
    basis,
    economics,
):
    """Research label only. Never emits GO/VETO."""
    values = {
        "cash": str(cash).upper(),
        "conversion": str(conversion).upper(),
        "efficiency": str(efficiency).upper(),
        "oi": str(oi).upper(),
        "liquidation": str(liquidation).upper(),
        "liquidity": str(liquidity).upper(),
        "independence": str(independence).upper(),
        "basis": str(basis).upper(),
        "economics": str(economics).upper(),
    }

    risks = []
    if wave_mature is True:
        risks.append("MATURE_WAVE")
    if values["liquidation"] in {"TAIL", "TAIL_CANDIDATE", "LIQUIDATION_TAIL"}:
        risks.append("LIQUIDATION_TAIL")
    if values["liquidity"] in {"ABSORPTION", "OVER_REFILL", "SWEEP_REJECTED"}:
        risks.append("ABSORPTION")
    if values["oi"] in {
        "BUILD_STALLED_OR_OPPOSING",
        "BUILD_OPPOSING_PRICE",
        "UNWIND_STALLED_OR_REJECTED",
    }:
        risks.append("OI_CONFLICT")
    if values["cash"] in {"OPPOSING", "DIVERGENT", "NO_ACCEPTANCE"}:
        risks.append("CASH_DIVERGENCE")
    if values["basis"] in {"DERIVATIVES_LED", "PERP_DISLOCATION_EXPANDING"} and values["cash"] != "CONVERTING":
        risks.append("DERIVATIVES_LED")
    if values["economics"] in {"FAIL", "RAW_FAIL", "COST_DOMINATED", "INSUFFICIENT_EDGE"}:
        risks.append("ECONOMIC_RISK")

    missing = sorted(k for k, v in values.items() if v in {"UNKNOWN", "STALE", "UNAVAILABLE", "INCOMPLETE"})

    if not bias_aligned:
        label = "BIAS_NOT_ALIGNED"
    elif risks:
        label = "RISK_EVIDENCE_PRESENT"
    elif missing:
        label = "EVIDENCE_INCOMPLETE"
    else:
        label = "CLEAN_CONTINUATION_CANDIDATE"

    return {
        "authority": False,
        "label": label,
        "risks": sorted(set(risks)),
        "missing": missing,
        "evidence": values,
    }

def wiring_map():
    """Existing repo resources to consume before adding new venues."""
    return {
        "bias": "bias_council + frozen bias thesis",
        "shared_wave_maturity": "ignition_core shared causal-wave state",
        "cash_acceptance": "entry_exchange_independence_hook + recorder cash",
        "flow_conversion": "entry_microstructure price/flow response",
        "flow_efficiency": "response_logic_reference.flow_efficiency",
        "oi_state": "ignition_core OI intent + response_logic_reference.oi_efficiency",
        "liquidation_phase": "forceOrder + OI + aggTrade + price progress",
        "liquidity_response": "recorder/liquidity_response.py + recorder/depth.py",
        "exchange_independence": "entry_exchange_independence_hook.py",
        "perp_basis": "mark/index/premium already collected",
        "economics": "entry_edge_tier + verified_cost_model",
    }
