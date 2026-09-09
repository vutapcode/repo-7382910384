"""Deterministic Guardian exit-action twins for offline same-WAL research.

The branches consume already available observations in availability order.
They never call the live Guardian, mutate a position, or select runtime policy.
"""

import hashlib
import json


VERSION = "GUARDIAN_ACTION_TWINS_V1"
BRANCHES = (
    "EXIT_AT_CHALLENGE",
    "EXIT_AT_TRANSFER_CONFIRMED",
    "HOLD_THROUGH_ONE_RECOVERY",
)


def _hash(value):
    body=json.dumps(
        value,sort_keys=True,separators=(",",":"),ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _number(value):
    try:return float(value)
    except (TypeError,ValueError):return None


def _eligible_exit(branch,state,recovery_seen):
    if branch=="EXIT_AT_CHALLENGE":
        return state in {
            "CHALLENGED","TRANSFER_CANDIDATE","TRANSFER_CONFIRMED",
            "RECOVERY_FAILED",
        }
    if branch=="EXIT_AT_TRANSFER_CONFIRMED":
        return state in {"TRANSFER_CONFIRMED","RECOVERY_FAILED"}
    if branch=="HOLD_THROUGH_ONE_RECOVERY":
        return state=="TRANSFER_CONFIRMED" or (
            recovery_seen and state=="RECOVERY_FAILED"
        )
    raise ValueError(f"UNKNOWN_GUARDIAN_TWIN:{branch}")


def replay_branch(
    branch, *, side, entry_price, frozen_cost_bps, observations,
    wal_identity, causal_wave_id, guardian_version,
):
    """Replay one exit policy without reading beyond its chosen exit row."""
    if branch not in BRANCHES:
        raise ValueError(f"UNKNOWN_GUARDIAN_TWIN:{branch}")
    side=str(side or "").upper()
    if side not in {"LONG","SHORT"}:
        raise ValueError("GUARDIAN_TWIN_SIDE_INVALID")
    entry=_number(entry_price)
    cost=_number(frozen_cost_bps)
    if entry is None or entry<=0.0 or cost is None or cost<0.0:
        raise ValueError("GUARDIAN_TWIN_ECONOMICS_INVALID")
    if not wal_identity or not causal_wave_id or not guardian_version:
        raise ValueError("GUARDIAN_TWIN_IDENTITY_INCOMPLETE")

    ordered=[]; previous=-1
    for raw in observations:
        row=dict(raw or {})
        available=int(row.get("available_time_ms",0) or 0)
        if available<=previous:
            raise ValueError("GUARDIAN_TWIN_AVAILABILITY_NOT_MONOTONIC")
        previous=available; ordered.append(row)

    recovery_seen=False; exit_row=None
    trace=[]
    for row in ordered:
        ledger=dict(row.get("adverse_wave_ledger") or {})
        state=str(ledger.get("state") or "UNKNOWN").upper()
        if state in {"RECOVERY_TEST","RECOVERED"}:
            recovery_seen=True
        trace.append({
            "available_time_ms":int(row["available_time_ms"]),
            "state":state,
            "observation_hash":ledger.get("last_observation_hash"),
        })
        if _eligible_exit(branch,state,recovery_seen):
            exit_row=row
            break

    exit_price=None; gross=None; net=None; status="UNRESOLVED_NO_EXIT"
    if exit_row is not None:
        bbo=dict(exit_row.get("executable_bbo") or {})
        exit_price=_number(bbo.get("bid" if side=="LONG" else "ask"))
        if exit_price is None or exit_price<=0.0:
            status="UNRESOLVED_NO_EXECUTABLE_BBO"
        else:
            sign=1.0 if side=="LONG" else -1.0
            gross=(exit_price-entry)/entry*10000.0*sign
            net=gross-cost
            status="EXECUTABLE_COUNTERFACTUAL"

    identity={
        "wal_identity":str(wal_identity),
        "causal_wave_id":str(causal_wave_id),
        "guardian_version":str(guardian_version),
        "side":side,"entry_price":round(entry,8),
        "frozen_cost_bps":round(cost,8),
    }
    result={
        "version":VERSION,"authority":False,"branch":branch,
        "identity":identity,"status":status,
        "exit_available_time_ms":int(exit_row["available_time_ms"])
        if exit_row is not None else None,
        "exit_price":round(exit_price,8) if exit_price is not None else None,
        "gross_pnl_bps":round(gross,8) if gross is not None else None,
        "net_pnl_bps_after_frozen_cost":round(net,8) if net is not None else None,
        "frozen_cost_applied_once":True,
        "no_lookahead":True,"trace":trace,
        "runtime_policy_selected":False,
    }
    result["deterministic_hash"]=_hash(result)
    return result


def replay_twins(**kwargs):
    """Run all branches on one immutable identity; never rank/promote them."""
    rows=[replay_branch(branch,**kwargs) for branch in BRANCHES]
    identities={_hash(row["identity"]) for row in rows}
    if len(identities)!=1:
        raise ValueError("GUARDIAN_TWIN_IDENTITY_MISMATCH")
    result={
        "version":VERSION,"authority":False,"branches":rows,
        "same_wal":True,"same_causal_wave":True,
        "same_guardian_version":True,"same_frozen_cost":True,
        "runtime_policy_selected":False,
    }
    result["deterministic_hash"]=_hash(result)
    return result
