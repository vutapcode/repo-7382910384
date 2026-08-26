"""Bias V2 reference. Research only; never live authority."""
AUTHORITY=False

def preserve_direction(raw_bias):
    x=str(raw_bias or "ABSTAIN").upper()
    return x if x in {"LONG","SHORT"} else "ABSTAIN"

def quality(*, raw_bias, phase, cash="UNKNOWN", efficiency="UNKNOWN",
            oi="UNKNOWN", liquidation="UNKNOWN", liquidity="UNKNOWN",
            basis="UNKNOWN", feed_health="CLEAN"):
    """Context may describe Bias quality; it must never create/flip direction."""
    direction=preserve_direction(raw_bias)
    risks=[]
    supports=[]

    if feed_health not in {"CLEAN","HEALTHY"}:
        risks.append("FEED_QUALITY")
    if phase=="ESTABLISHED_TREND":
        supports.append("ESTABLISHED_CONTEXT")
    elif phase=="REVERSAL_CANDIDATE":
        risks.append("REVERSAL_CANDIDATE")

    if cash in {"CONVERTING","BROAD_CASH_ACCEPTANCE"}:
        supports.append("CASH_ACCEPTANCE")
    elif cash in {"OPPOSING","DIVERGENT","NO_ACCEPTANCE"}:
        risks.append("CASH_DIVERGENCE")

    if efficiency in {"IMPROVING","RISING"}:
        supports.append("FLOW_EFFICIENCY")
    elif efficiency in {"DECAY","DECAYING","COLLAPSING"}:
        risks.append("FLOW_EXHAUSTION")

    # OI is causal context only, never direction.
    if oi in {"POSITION_BUILD","BUILD_CONVERTING"}:
        supports.append("POSITION_BUILD")
    elif oi in {"UNWIND","UNWIND_CONVERTING"}:
        risks.append("UNWIND_CONTEXT")

    # forceOrder is sampled evidence, not a complete liquidation tape.
    if liquidation in {"TAIL","TAIL_CANDIDATE","LIQUIDATION_TAIL"}:
        risks.append("LIQUIDATION_TAIL")
    if liquidity in {"ABSORPTION","OVER_REFILL","SWEEP_REJECTED"}:
        risks.append("ABSORPTION")
    if basis in {"DERIVATIVES_LED","PERP_DISLOCATION_EXPANDING"}:
        risks.append("DERIVATIVES_DISTORTION")

    return {
        "authority":False,
        "direction":direction,
        "phase":phase,
        "supports":sorted(set(supports)),
        "risks":sorted(set(risks)),
    }

def fast_4s_policy(fast_side, slow_side):
    """4s may journal support/conflict only; never acquire or flip Bias."""
    f=str(fast_side or "ABSTAIN").upper()
    s=str(slow_side or "ABSTAIN").upper()
    if f=="ABSTAIN":
        label="FAST_NEUTRAL"
    elif s in {"LONG","SHORT"} and f==s:
        label="FAST_SUPPORTS_CONTEXT"
    elif s in {"LONG","SHORT"}:
        label="FAST_OPPOSES_CONTEXT"
    else:
        label="FAST_WITHOUT_CONTEXT"
    return {"authority":False,"label":label,"may_flip_bias":False}

WIRING={
    "direction":"2_suy_luan_mapping/bias_council.py: 180s/60s/15s cash spine",
    "fast":"4s telemetry only",
    "efficiency":"response_logic_reference.flow_efficiency",
    "oi":"existing OI freshness/state",
    "liquidation":"forceOrder + OI + aggTrade research context",
    "liquidity":"recorder/liquidity_response.py + recorder/depth.py",
    "basis":"existing mark/index/premium",
}
