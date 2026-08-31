"""Version-bound empirical Guardian economics for Entry Economics V3.

The model never converts MFE into alpha.  It learns only executable shadow
positions closed by the active Guardian and stores net bps after the frozen
execution cost.  Unknown cohorts remain bootstrap telemetry.
"""

import math


VERSION = "ENTRY_ECONOMICS_V8_TIME_TO_EVENT"
CONTRACT_VERSION = "ENTRY_ECONOMICS_V8_TIME_TO_EVENT"
MAX_ROWS = 1024
EXACT_MIN = 30
PARENT_MIN = 50
TIME_MIN_WINNERS = 10


def _u(value, default="UNKNOWN"):
    return str(value or default).upper()


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def consumed_band(value):
    value = _f(value, 1.0)
    if value <= 0.15:
        return "EARLY_0_15"
    if value <= 0.25:
        return "EARLY_15_25"
    if value <= 0.35:
        return "EARLY_25_35"
    return "MATURE"


def feature_snapshot(result, regime, execution_style, thesis_audit=None):
    ignition = dict((result or {}).get("ignition") or {})
    frozen = dict(ignition.get("bias_snapshot") or {})
    context = dict(frozen.get("direction_context") or {})
    flow = dict(ignition.get("flow_efficiency") or {})
    venues = dict(flow.get("venues") or {})
    proposer = _u(ignition.get("proposer"))
    primary_state = _u((venues.get(proposer.lower()) or {}).get("state"))
    questions = dict((thesis_audit or {}).get("questions") or {})
    composite_flow = dict(questions.get("q3_flow_efficiency") or {})
    composite_state = _u(composite_flow.get("status"), primary_state)
    transition = dict(ignition.get("transition_authority") or {})
    transition_class = (
        "FAST_REVERSAL_CONFIRMED"
        if ignition.get("transition_confirmed")
        and transition.get("status") == "REVERSAL_CONFIRMED"
        else "BACKGROUND_ALIGNED"
    )
    oi = dict(ignition.get("oi_verification_state") or {})
    if not oi:
        raw_oi = dict(ignition.get("oi_intent") or {})
        oi_status = "UNKNOWN" if not raw_oi.get("fresh") else _u(raw_oi.get("intent"))
    else:
        oi_status = _u(oi.get("status"))
    return {
        "economic_contract_version": CONTRACT_VERSION,
        "side": _u((result or {}).get("side")),
        "entry_mode": _u((result or {}).get("entry_mode"), "IGNITION"),
        "regime": _u((regime or {}).get("regime"), "NORMAL"),
        "proof_type": _u(ignition.get("proof_type")),
        "proposer": proposer,
        "execution_style": _u(execution_style),
        "bias_phase": _u(context.get("phase")),
        "consumed_band": consumed_band(ignition.get("consumed_fraction")),
        "oi_quality": oi_status,
        # Cohorts must learn the state that actually authorized Entry.  When
        # the proposer is ambiguous but an independent cash venue confirms,
        # storing only the proposer state poisons the empirical cohort.
        "flow_efficiency_state": composite_state,
        "primary_flow_efficiency_state": primary_state,
        "flow_confirmation_source": _u(
            composite_flow.get("confirmation_source"), "NONE"
        ),
        "transition_class": transition_class,
        "cross_venue_flow_witnesses": tuple(
            sorted(composite_flow.get("cross_venue_witness_venues") or ())
        ),
    }


def _rows(state):
    rows = getattr(state, "_entry_economics_v2_rows", None)
    if not isinstance(rows, list):
        rows = []
        state._entry_economics_v2_rows = rows
    rows[:] = [
        dict(row) for row in rows
        if isinstance(row, dict)
        and row.get("economic_contract_version") == CONTRACT_VERSION
        and row.get("valid") is True
    ][-MAX_ROWS:]
    return rows


def _exact_key(snapshot):
    names = (
        "side", "entry_mode", "regime", "proof_type", "proposer",
        "execution_style", "bias_phase", "consumed_band", "oi_quality",
        "flow_efficiency_state", "transition_class",
    )
    return tuple(_u(snapshot.get(name)) for name in names)


def _parent_key(snapshot):
    return tuple(_u(snapshot.get(name)) for name in (
        "side", "proof_type", "proposer", "execution_style",
        "flow_efficiency_state", "transition_class",
    ))


def record(state, snapshot, *, net_bps, execution_cost_bps,
           time_to_positive_net_seconds=None, observation_seconds=None,
           valid=True):
    snapshot = dict(snapshot or {})
    if snapshot.get("economic_contract_version") != CONTRACT_VERSION:
        return None
    event_time = (
        None if time_to_positive_net_seconds is None
        else max(0.0, float(time_to_positive_net_seconds))
    )
    observed_for = (
        event_time if observation_seconds is None and event_time is not None
        else None if observation_seconds is None
        else max(0.0, float(observation_seconds))
    )
    row = {
        **snapshot,
        "net_pnl_bps_after_frozen_cost": float(net_bps),
        "execution_cost_bps": float(execution_cost_bps),
        "time_to_positive_net_seconds": event_time,
        "time_to_positive_net_observation_seconds": observed_for,
        "time_to_positive_net_event": event_time is not None,
        "time_to_positive_net_termination": (
            "FIRST_POSITIVE_NET" if event_time is not None
            else "GUARDIAN_CLOSE_BEFORE_POSITIVE"
        ),
        "valid": bool(valid),
        "code_version": str(getattr(state, "code_version", "") or ""),
        "config_version": str(getattr(state, "strategy_config_version", "") or ""),
    }
    if not row["valid"]:
        return None
    rows = _rows(state)
    rows.append(row)
    if len(rows) > MAX_ROWS:
        del rows[:-MAX_ROWS]
    state.entry_economics_v2_last = row
    return row


def _stats(rows):
    values = sorted(float(row["net_pnl_bps_after_frozen_cost"]) for row in rows)
    trim = max(1, int(len(values) * 0.10))
    core = values[trim:-trim] if len(values) > 2 * trim else values
    mean = sum(core) / len(core)
    variance = sum((value - mean) ** 2 for value in core) / max(1, len(core) - 1)
    stderr = math.sqrt(max(0.0, variance)) / math.sqrt(max(1, len(core)))
    lcb = mean - 1.645 * stderr
    events = [
        float(row["time_to_positive_net_seconds"])
        for row in rows
        if row.get("time_to_positive_net_event") is True
        and row.get("time_to_positive_net_seconds") is not None
    ]
    competing = sum(
        row.get("time_to_positive_net_event") is False for row in rows
    )
    event_fraction = len(events) / len(rows)
    # This is a competing-event empirical incidence, not a winner-conditioned
    # percentile and not a fabricated survival probability.  P80 is only
    # identifiable when at least 80% of the complete cohort actually reached
    # positive net before Guardian termination.
    p80 = None
    if len(events) >= TIME_MIN_WINNERS and event_fraction >= 0.80:
        event_rank = max(0, min(len(events) - 1, math.ceil(0.80 * len(rows)) - 1))
        p80 = sorted(events)[event_rank]
    return {
        "samples": len(rows),
        "expected_guardian_net_bps": round(mean, 6),
        "lower_confidence_bound_bps": round(lcb, 6),
        "win_rate": round(sum(value > 0.0 for value in values) / len(values), 6),
        "positive_net": bool(mean > 0.0 and lcb >= 0.0),
        "time_to_positive_net_events": len(events),
        "time_to_positive_net_competing_terminations": competing,
        "time_to_positive_net_event_fraction": round(event_fraction, 6),
        "time_to_positive_net_calibration_status": (
            "IDENTIFIED" if p80 is not None
            else "UNRESOLVED_COMPETING_TERMINATION"
        ),
        "time_to_positive_net_p80_seconds": (
            round(p80, 6) if p80 is not None else None
        ),
    }


def estimate(state, snapshot):
    snapshot = dict(snapshot or {})
    exact_key = _exact_key(snapshot)
    parent_key = _parent_key(snapshot)
    rows = _rows(state)
    exact = [row for row in rows if _exact_key(row) == exact_key]
    # Hierarchical backoff must contribute independent evidence. The queried
    # exact cohort is excluded from its parent instead of being counted twice.
    parent = [
        row for row in rows
        if _parent_key(row) == parent_key and _exact_key(row) != exact_key
    ]
    if len(exact) >= EXACT_MIN:
        selected, level, minimum = exact, "EXACT", EXACT_MIN
    elif len(parent) >= PARENT_MIN:
        selected, level, minimum = parent, "PARENT", PARENT_MIN
    else:
        return {
            "version": VERSION,
            "economic_contract_version": CONTRACT_VERSION,
            "status": "BOOTSTRAP_UNVERIFIED",
            "level": "NONE",
            "samples": len(exact),
            "parent_samples": len(parent),
            "minimum_samples": EXACT_MIN,
            "expected_guardian_net_bps": None,
            "lower_confidence_bound_bps": None,
            "time_to_positive_net_p80_seconds": None,
            "time_to_positive_net_events": 0,
            "time_to_positive_net_competing_terminations": 0,
            "time_to_positive_net_calibration_status": "UNRESOLVED_NO_COHORT",
            "authority": False,
            "exact_key": "|".join(exact_key),
            "parent_key": "|".join(parent_key),
        }
    report = _stats(selected)
    replay_approved = bool(
        getattr(state, "entry_economics_v6_replay_approved", False)
    )
    report.update({
        "version": VERSION,
        "economic_contract_version": CONTRACT_VERSION,
        "status": "ACTIVE" if replay_approved else "REPLAY_PENDING",
        "level": level,
        "minimum_samples": minimum,
        "parent_samples": len(parent),
        "authority": replay_approved,
        "exact_key": "|".join(exact_key),
        "parent_key": "|".join(parent_key),
        "policy": "EXECUTABLE_GUARDIAN_NET_NOT_MFE_ALPHA",
    })
    return report
