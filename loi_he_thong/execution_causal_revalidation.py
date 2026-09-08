"""Read-only causal checks at the Ignition -> execution boundary.

This module never evaluates Ignition, creates episodes, mutates Bias, or
authorizes a strategy candidate.  It only decides whether an already reserved
candidate is still executable after REST/maker latency.
"""

from loi_he_thong import authority_contracts
from loi_he_thong import execution_contradiction_shadow
from loi_he_thong import ignition_signals
from loi_he_thong import ignition_core
from loi_he_thong import verified_cost_model


VERSION = "EXECUTION_CAUSAL_REVALIDATION_V4_SEALED_AUTHORITY"
PROOF_MAX_AGE_SECONDS = 1.5
BBO_MAX_AGE_SECONDS = 1.0
FOLLOW_MAX_MS = 600


def execution_contract(ok, reason, causal_episode_id=None, *, pending=False):
    """Serialize this owner's conclusion without reinterpreting market truth."""
    reason = str(reason or "UNKNOWN").upper()
    if pending:
        action = "EXECUTION_UNKNOWN"
    elif ok:
        action = "EXECUTE"
    elif any(token in reason for token in (
        "OPPOSING", "REVERSAL", "AUTHORITY_", "EPISODE_CHANGED",
        "OPPORTUNITY_CHANGED", "ALREADY_CONSUMED", "IMPULSE_ALREADY_CONSUMED",
    )):
        action = "CANCEL"
    else:
        action = "EXECUTION_UNKNOWN"
    return authority_contracts.seal(
        "EXECUTION", "EXECUTION_CAUSAL_REVALIDATION",
        causal_episode_id,
        {"execution_action": action, "execution_reason": reason},
    )


def pending_contract(causal_episode_id=None):
    return execution_contract(
        False, "NOT_EVALUATED_AT_DECISION", causal_episode_id, pending=True,
    )


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _engine(state):
    value = getattr(state, "_ignition_signal_engine", None)
    venues = getattr(value, "venues", None)
    return value if isinstance(venues, dict) else None


def _reservation(state):
    value = getattr(state, "canonical_reserved_context", None)
    return value if isinstance(value, dict) else {}


def _authority_contract(state, side, result, now):
    """Validate the immutable proof dependencies without rerunning strategy."""
    reserved = _reservation(state)
    basis = str((result or {}).get("authority_basis") or "").upper()
    dependencies = dict(
        (result or {}).get("authority_dependencies") or {}
    )
    proof_hash = str((result or {}).get("authority_proof_hash") or "")
    if basis not in {"BIAS_ALIGNED", "TRANSITION_CONFIRMED"}:
        return False, "AUTHORITY_BASIS_INVALID", {}
    if not dependencies or not proof_hash:
        return False, "AUTHORITY_PROOF_MISSING", {}
    if basis != str(reserved.get("authority_basis") or "").upper():
        return False, "RESERVED_AUTHORITY_BASIS_CHANGED", {}
    if dependencies != dict(reserved.get("authority_dependencies") or {}):
        return False, "RESERVED_AUTHORITY_DEPENDENCIES_CHANGED", {}
    if proof_hash != str(reserved.get("authority_proof_hash") or ""):
        return False, "RESERVED_AUTHORITY_PROOF_CHANGED", {}
    sealed_ok, sealed_reason, sealed_detail = (
        ignition_core.validate_frozen_authority({
            **dict(result or {}), "side": str(side).upper(),
        })
    )
    if not sealed_ok:
        return False, sealed_reason, sealed_detail

    cash = dict(dependencies.get("current_cash_conversion") or {})
    qualified = dict(cash.get("qualified_acceptances") or {})
    now_ms = int(float(now) * 1000.0)
    fresh = []
    for venue, row in sorted(qualified.items()):
        row = dict(row or {})
        accepted_at = int(row.get("accepted_at_ms", 0) or 0)
        valid_until = int(row.get("valid_until_ms", 0) or 0)
        if accepted_at <= now_ms <= valid_until:
            fresh.append(str(venue))
    minimum = int(cash.get("minimum_fresh_venues", 0) or 0)
    if minimum <= 0 or len(fresh) < minimum:
        return False, "CURRENT_CASH_AUTHORITY_EXPIRED", {
            "fresh_cash_venues": fresh,
            "minimum_fresh_venues": minimum,
        }

    if basis == "TRANSITION_CONFIRMED":
        transition = dict(dependencies.get("transition") or {})
        accepted = {
            str(value) for value in transition.get(
                "accepted_cash_venues", ()
            )
        }
        if not (
            transition.get("old_side_failure")
            and transition.get(
                "new_side_cash_acceptance",
                transition.get("new_side_cash_control"),
            )
            and transition.get("dual_cash_acceptance")
            and {"binance_spot", "coinbase_spot"}.issubset(accepted)
            and {"binance_spot", "coinbase_spot"}.issubset(set(fresh))
        ):
            return False, "SEALED_TRANSITION_PROOF_INVALID", {}
    return True, "PASS", {
        **dict(sealed_detail or {}),
        "authority_basis": basis,
        "fresh_cash_venues": fresh,
    }


def _material(row):
    venue = str((row or {}).get("venue") or "")
    return bool(
        venue in ignition_signals.MIN_QTY
        and _f(row.get("total_qty")) >= ignition_signals.MIN_QTY[venue]
        and abs(_f(row.get("imbalance"))) >= 0.20
        and abs(_f(row.get("price_conversion_bps"))) >= 0.15
        and bool(row.get("clock_valid"))
    )


def _rows_after(state, result, cutoff_seconds=None):
    """Return buckets that started fully after the causal cutoff."""
    engine = _engine(state)
    if engine is None:
        return None
    cutoff_raw = int(max(
        _f((result or {}).get("ts")), _f(cutoff_seconds)
    ) * 1000.0)
    # A finalized 100 ms bucket containing the decision/placement instant may
    # also contain older events. Start at the next bucket boundary so such a
    # straddling bucket can never become post-decision evidence.
    cutoff = (
        cutoff_raw // ignition_signals.BUCKET_MS + 1
    ) * ignition_signals.BUCKET_MS
    return {
        name: tuple(
            row for row in venue.history
            if int(row.get("bucket_start_ms", 0) or 0) >= cutoff
        )
        for name, venue in engine.venues.items()
    }


def _required_venues(result):
    ignition = (result or {}).get("ignition") or {}
    names = set(ignition.get("cash_venues") or ())
    names.add("futures")
    return names


def _epoch_ok(state, result):
    engine = _engine(state)
    if engine is None:
        return False, "EXECUTED_FLOW_ENGINE_UNAVAILABLE", {}
    reserved = _reservation(state)
    expected = dict(reserved.get("epochs") or {})
    if not expected:
        expected = {
            str(name): int((row or {}).get("epoch", 0) or 0)
            for name, row in (
                ((result or {}).get("ignition") or {}).get("clock_quality") or {}
            ).items()
            if isinstance(row, dict) and int((row or {}).get("epoch", 0) or 0) > 0
        }
    for name in sorted(_required_venues(result)):
        venue = engine.venues.get(name)
        if venue is None:
            return False, "EXECUTED_FLOW_VENUE_UNAVAILABLE", {"venue": name}
        if not bool(venue.clock_valid):
            return False, "EXECUTED_FLOW_CLOCK_INVALID", {"venue": name}
        if name in expected and int(venue.epoch) != int(expected[name]):
            return False, "EXECUTED_FLOW_EPOCH_RESET", {
                "venue": name,
                "expected_epoch": int(expected[name]),
                "current_epoch": int(venue.epoch),
            }
    if bool(getattr(state, "shadow_data_gap_active", False)):
        return False, "FUTURES_EXECUTED_FLOW_GAP_ACTIVE", {}
    return True, "PASS", {}


def _material_streak(rows, side, *, minimum=2):
    """Find adjacent material 100 ms buckets; isolated jerks have no authority."""
    side = str(side or "").upper()
    streak = []
    previous = None
    for row in rows:
        bucket = int(row.get("bucket_start_ms", 0) or 0)
        aligned = bool(str(row.get("side", "")).upper() == side and _material(row))
        if not aligned:
            streak, previous = [], None
            continue
        if previous is None or bucket - previous != ignition_signals.BUCKET_MS:
            streak = [row]
        else:
            streak.append(row)
        previous = bucket
        if len(streak) >= minimum:
            return tuple(streak[-minimum:])
    return ()


def _opposing_ok(rows, side):
    """Submit-safety contradiction guard, never a second Entry council."""
    opposing = "SHORT" if str(side).upper() == "LONG" else "LONG"
    futures_only_opposition = None
    for venue in ("binance_spot", "coinbase_spot", "futures"):
        streak = _material_streak(rows.get(venue, ()), opposing)
        if streak:
            if venue == "futures":
                futures_only_opposition = {
                    "venue": venue,
                    "side": opposing,
                    "buckets": [
                        int(row.get("bucket_start_ms", 0) or 0)
                        for row in streak
                    ],
                    "authority": "DERIVATIVES_CONTEXT_ONLY",
                }
                continue
            return False, "POST_PROOF_OPPOSING_FLOW_2_BUCKETS", {
                "venue": venue,
                "buckets": [int(row.get("bucket_start_ms", 0) or 0) for row in streak],
            }
        if venue == "futures":
            continue
        venue_rows = rows.get(venue, ())
        adverse_price = tuple(
            row for row in venue_rows
            if bool(row.get("clock_valid"))
            and (
                str(side).upper() == "LONG"
                and _f(row.get("price_conversion_bps")) <= -0.15
                or str(side).upper() == "SHORT"
                and _f(row.get("price_conversion_bps")) >= 0.15
            )
        )
        opposing_flow = tuple(
            row for row in venue_rows
            if str(row.get("side", "")).upper() == opposing
            and _f(row.get("total_qty")) >= ignition_signals.MIN_QTY[venue]
            and abs(_f(row.get("imbalance"))) >= 0.20
            and bool(row.get("clock_valid"))
        )
        for price_row in adverse_price:
            price_bucket = int(price_row.get("bucket_start_ms", 0) or 0)
            coherent_flow = next((
                flow_row for flow_row in opposing_flow
                if 0 < abs(
                    int(flow_row.get("bucket_start_ms", 0) or 0)
                    - price_bucket
                ) <= ignition_core.EVIDENCE_GAP_MS
            ), None)
            if coherent_flow is not None:
                flow_bucket = int(
                    coherent_flow.get("bucket_start_ms", 0) or 0
                )
                return False, "POST_PROOF_CASH_PRICE_FLOW_REVERSAL", {
                    "venue": venue,
                    "price_bucket": price_bucket,
                    "flow_bucket": flow_bucket,
                    "coherence_gap_ms": abs(flow_bucket - price_bucket),
                    "coherence_limit_ms": ignition_core.EVIDENCE_GAP_MS,
                }
    return True, "PASS", {
        "futures_only_opposition": futures_only_opposition,
    } if futures_only_opposition else {}


def _submit_timing_telemetry(state, side, result, now):
    """Observe GO -> submit decay through Ignition's shared representation."""
    result = result or {}
    dependencies = dict(result.get("authority_dependencies") or {})
    decision_ts = _f(result.get("ts"))
    now_ms = int(float(now) * 1000.0)
    ignition = dict(result.get("ignition") or {})
    go_state = str(
        dependencies.get("flow_state_at_go")
        or ignition_core.flow_efficiency_state(
            ignition.get("flow_efficiency") or {},
            ignition.get("proposer"), ignition.get("cash_venues"),
        ).get("state")
        or "UNKNOWN"
    ).upper()
    engine = _engine(state)
    submit_snapshot = {}
    submit_state = "UNKNOWN"
    if engine is not None:
        histories = {
            name: tuple(venue.history)
            for name, venue in engine.venues.items()
        }
        frozen_wave = dict(ignition.get("causal_wave_snapshot") or {})
        ledger = dict(ignition.get("bounded_wave_ledger") or {})
        submit_snapshot = ignition_core.causal_wave_snapshot(
            histories, side, now_ms,
            causal_wave_id=result.get("causal_episode_id"),
            wave_onset_ms=(
                frozen_wave.get("wave_onset_ms")
                or ledger.get("wave_started_at_ms")
            ),
            primary_cash=ignition.get("proposer"),
            cash_venues=ignition.get("cash_venues"),
        )
        submit_state = str(
            submit_snapshot.get("flow_efficiency_state") or "UNKNOWN"
        ).upper()
    qualified = dict(
        (dependencies.get("current_cash_conversion") or {}).get(
            "qualified_acceptances"
        ) or {}
    )
    cash_age_by_venue = {}
    for venue, row in sorted(qualified.items()):
        accepted_at = int((row or {}).get("accepted_at_ms", 0) or 0)
        if accepted_at > 0:
            cash_age_by_venue[str(venue)] = max(0, now_ms - accepted_at)
    cash_age = max(cash_age_by_venue.values(), default=None)
    decayed = bool(
        go_state == "CONTINUING_CONFIRMED"
        and submit_state in {
            "FADING", "DECAYING", "REACCELERATION_UNCONFIRMED", "UNKNOWN",
        }
    )
    return {
        "version": "GO_SUBMIT_TIMING_V1",
        "decision_to_submit_ms": (
            round(max(0.0, (float(now) - decision_ts) * 1000.0), 3)
            if decision_ts > 0.0 else None
        ),
        "flow_state_at_GO": go_state,
        "flow_state_at_submit": submit_state,
        "cash_age_at_submit": cash_age,
        "cash_age_at_submit_ms": cash_age,
        "cash_age_by_venue_ms": cash_age_by_venue,
        "flow_decayed_before_submit": decayed,
        "causal_wave_snapshot_at_submit": submit_snapshot,
        "authority": False,
        "policy": "TELEMETRY_ONLY_SHARED_IGNITION_CLASSIFIER",
    }


def validate_submit(state, side, result, now):
    """Fail closed when an already-reserved GO is no longer the same thesis."""
    result = result or {}
    side = str(side or "").upper()
    now = float(now)
    if side not in ("LONG", "SHORT"):
        return False, "SIDE_INVALID", {}

    timing = _submit_timing_telemetry(state, side, result, now)
    physical_facts = {
        name: None for name in execution_contradiction_shadow.FAIL_CLOSED_FACTS
    }

    def verdict(ok, reason, detail=None):
        detail = dict(detail or {})
        contradiction = None
        if reason == "POST_PROOF_OPPOSING_FLOW_2_BUCKETS":
            venue = str(detail.get("venue") or "")
            contradiction = {
                "kind": (
                    "OPPOSING_FLOW" if venue == "futures"
                    else "CASH_PRICE_FLOW_REVERSAL"
                ),
                "venues": [venue] if venue else [],
            }
        elif reason == "POST_PROOF_CASH_PRICE_FLOW_REVERSAL":
            venue = str(detail.get("venue") or "")
            contradiction = {
                "kind": "CASH_PRICE_FLOW_REVERSAL",
                "venues": [venue] if venue else [],
            }
        elif detail.get("futures_only_opposition"):
            contradiction = {
                "kind": "OPPOSING_FLOW",
                "venues": ["futures"],
            }
        facts = dict(physical_facts)
        if contradiction is not None:
            facts["post_go_contradiction"] = contradiction
        comparison = execution_contradiction_shadow.observe_active(
            ok, reason, facts, side=side,
        )
        return bool(ok), str(reason), {
            **timing,
            **detail,
            "phase6_execution_shadow": comparison,
            "execution_contract": execution_contract(
                ok, reason, (result or {}).get("causal_episode_id")
            ),
            "post_proof_guard_policy": (
                "CONTRADICTION_ONLY_NO_STRATEGY_REINTERPRETATION"
            ),
        }

    reserved = _reservation(state)
    if not reserved:
        return verdict(False, "CANONICAL_RESERVATION_MISSING")
    physical_facts["reservation_ok"] = True
    if int(reserved.get("opportunity_id", 0) or 0) != int(
        result.get("canonical_opportunity_id", 0) or 0
    ):
        return verdict(False, "CANONICAL_OPPORTUNITY_CHANGED")
    physical_facts["opportunity_identity_ok"] = True
    if str(reserved.get("causal_episode_id") or "") != str(
        result.get("causal_episode_id") or ""
    ):
        return verdict(False, "CAUSAL_EPISODE_CHANGED")
    physical_facts["causal_episode_identity_ok"] = True

    ok, reason, authority_detail = _authority_contract(
        state, side, result, now,
    )
    if not ok:
        return verdict(ok, reason, authority_detail)
    physical_facts["sealed_handoff_ok"] = True

    decision_ts = _f(result.get("ts"))
    proof_age = now - decision_ts
    if decision_ts <= 0.0 or proof_age < 0.0 or proof_age > PROOF_MAX_AGE_SECONDS:
        return verdict(
            False, "CAUSAL_PROOF_STALE",
            {"age_seconds": max(0.0, proof_age)},
        )

    bid = _f(getattr(state, "execution_best_bid", 0.0))
    ask = _f(getattr(state, "execution_best_ask", 0.0))
    bbo_age = now - _f(getattr(state, "execution_price_time", 0.0))
    if bid <= 0.0 or ask <= bid:
        return verdict(False, "EXECUTION_BBO_INVALID", {"bid": bid, "ask": ask})
    physical_facts["bbo_valid"] = True
    if bbo_age < 0.0 or bbo_age > BBO_MAX_AGE_SECONDS:
        return verdict(
            False, "EXECUTION_BBO_STALE",
            {"age_seconds": max(0.0, bbo_age)},
        )
    physical_facts["bbo_fresh"] = True

    ok, reason, detail = _epoch_ok(state, result)
    if not ok:
        return verdict(ok, reason, detail)
    physical_facts["epoch_ok"] = True
    physical_facts["flow_gap_free"] = True
    physical_facts["flow_clock_valid"] = True
    rows = _rows_after(state, result)
    if rows is None:
        return verdict(False, "EXECUTED_FLOW_ENGINE_UNAVAILABLE")
    physical_facts["post_go_observation_available"] = True
    ok, reason, detail = _opposing_ok(rows, side)
    if not ok:
        return verdict(ok, reason, detail)
    return verdict(True, "PASS", {
        **authority_detail,
        **detail,
        "proof_age_seconds": round(proof_age, 6),
        "bbo_age_seconds": round(bbo_age, 6),
        "post_result_rows": {name: len(value) for name, value in rows.items()},
    })


def _current_release(state, side, result, placed_at):
    """Observe a fresh release without reinterpreting Entry strategy.

    Two persistent independent cash roots are sufficient. A single cash root
    retains the existing Futures echo requirement. This mirrors the causal
    truth accepted by Ignition and keeps Futures from vetoing dual-cash control.
    """
    rows = _rows_after(state, result, cutoff_seconds=placed_at)
    if rows is None:
        return False, "EXECUTED_FLOW_ENGINE_UNAVAILABLE", {}
    side = str(side).upper()
    ignition = (result or {}).get("ignition") or {}
    cash_streaks = {
        venue: _material_streak(rows.get(venue, ()), side)
        for venue in ("binance_spot", "coinbase_spot")
    }
    if all(cash_streaks.values()):
        first_ms = {
            venue: int(streak[0].get("receive_time_ms", 0) or 0)
            for venue, streak in cash_streaks.items()
        }
        if abs(first_ms["binance_spot"] - first_ms["coinbase_spot"]) <= FOLLOW_MAX_MS:
            return True, "CURRENT_DUAL_CASH_RELEASE", {
                "cash_venues": ["binance_spot", "coinbase_spot"],
                "cash_buckets": {
                    venue: [
                        int(row.get("bucket_start_ms", 0) or 0)
                        for row in streak
                    ]
                    for venue, streak in cash_streaks.items()
                },
                "cross_cash_span_ms": abs(
                    first_ms["binance_spot"] - first_ms["coinbase_spot"]
                ),
            }
    for venue in ignition.get("cash_venues") or ():
        cash = cash_streaks.get(venue) or _material_streak(
            rows.get(venue, ()), side
        )
        if not cash:
            continue
        first_cash_ms = int(cash[0].get("receive_time_ms", 0) or 0)
        futures = tuple(
            row for row in rows.get("futures", ())
            if str(row.get("side", "")).upper() == side
            and _material(row)
            and first_cash_ms <= int(row.get("receive_time_ms", 0) or 0)
            <= first_cash_ms + FOLLOW_MAX_MS
        )
        if not futures:
            continue
        if _f(cash[-1].get("flow_acceleration")) < 0.0:
            continue
        return True, "CURRENT_CASH_FUTURES_RELEASE", {
            "cash_venue": venue,
            "cash_buckets": [int(row.get("bucket_start_ms", 0) or 0) for row in cash],
            "futures_response_ms": int(futures[0].get("receive_time_ms", 0) or 0) - first_cash_ms,
        }
    return False, "CURRENT_RELEASE_NOT_PROVED", {}


def maker_ttl_release(state, side, result, now, placed_at):
    """Shadow-only physical/cost recheck for the reserved Action decision.

    Entry already owned phase and remaining-edge judgment. Execution checks
    freshness, contradiction, executable release and cost only; it must not
    recompute consumed phase from a later price sample.
    """
    episode_id = (result or {}).get("causal_episode_id")

    def verdict(ok, reason, detail=None):
        detail = dict(detail or {})
        detail["execution_contract"] = execution_contract(
            ok, reason, episode_id
        )
        return bool(ok), str(reason), detail

    ok, reason, detail = validate_submit(state, side, result, now)
    if not ok:
        return ok, reason, detail
    ok, reason, detail = _current_release(state, side, result, placed_at)
    if not ok:
        return verdict(ok, reason, detail)
    cost_ok, cost_reason, cost_detail = (
        verified_cost_model.validate_execution_cost_contract(
            result, state, "TAKER"
        )
    )
    if not cost_ok:
        return verdict(False, cost_reason, cost_detail)
    return verdict(True, "CURRENT_RELEASE_PASS", {
        **detail,
        "phase_owner": "ENTRY_ACTION_FROZEN_AT_GO",
        "current_execution_cost_bps": cost_detail["current_cost_bps"],
        "cost_budget_bps": cost_detail["budget_bps"],
        "cost_components": cost_detail["current"],
    })
