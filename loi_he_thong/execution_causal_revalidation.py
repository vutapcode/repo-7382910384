"""Read-only causal checks at the Ignition -> execution boundary.

This module never evaluates Ignition, creates episodes, mutates Bias, or
authorizes a strategy candidate.  It only decides whether an already reserved
candidate is still executable after REST/maker latency.
"""

import hashlib
import json

from loi_he_thong import ignition_signals
from loi_he_thong import ignition_core
from loi_he_thong import verified_cost_model


VERSION = "EXECUTION_CAUSAL_REVALIDATION_V2_COHERENT_REVERSAL"
PROOF_MAX_AGE_SECONDS = 1.5
BBO_MAX_AGE_SECONDS = 1.0
BIAS_MAX_AGE_SECONDS = 3.0
BIAS_MIN_CONFIDENCE = 0.55
FOLLOW_MAX_MS = 600


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
    encoded = json.dumps(
        {"authority_basis": basis, "authority_dependencies": dependencies},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != proof_hash:
        return False, "AUTHORITY_PROOF_HASH_INVALID", {}
    if str(dependencies.get("side") or "").upper() != str(side).upper():
        return False, "AUTHORITY_SIDE_CHANGED", {}
    if str(dependencies.get("causal_episode_id") or "") != str(
        (result or {}).get("causal_episode_id") or ""
    ):
        return False, "AUTHORITY_EPISODE_CHANGED", {}

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

    if basis == "BIAS_ALIGNED":
        if str(
            getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN"
        ).upper() != str(side).upper():
            return False, "BIAS_SIDE_CHANGED", {}
        if _f(getattr(state, "bias_confidence", 0.0)) < BIAS_MIN_CONFIDENCE:
            return False, "BIAS_CONFIDENCE_DROPPED", {}
        bias_age = float(now) - _f(getattr(state, "bias_updated_at", 0.0))
        if bias_age < 0.0 or bias_age > BIAS_MAX_AGE_SECONDS:
            return False, "BIAS_STALE", {
                "age_seconds": max(0.0, bias_age)
            }
    else:
        transition = dict(dependencies.get("transition") or {})
        accepted = {
            str(value) for value in transition.get(
                "accepted_cash_venues", ()
            )
        }
        if not (
            transition.get("old_side_failure")
            and transition.get("new_side_cash_control")
            and transition.get("dual_cash_acceptance")
            and {"binance_spot", "coinbase_spot"}.issubset(accepted)
            and {"binance_spot", "coinbase_spot"}.issubset(set(fresh))
        ):
            return False, "TRANSITION_AUTHORITY_DEPENDENCY_INVALID", {}
    return True, "PASS", {
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
    opposing = "SHORT" if str(side).upper() == "LONG" else "LONG"
    for venue in ("binance_spot", "coinbase_spot", "futures"):
        streak = _material_streak(rows.get(venue, ()), opposing)
        if streak:
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
    return True, "PASS", {}


def validate_submit(state, side, result, now):
    """Fail closed when an already-reserved GO is no longer the same thesis."""
    result = result or {}
    side = str(side or "").upper()
    now = float(now)
    if side not in ("LONG", "SHORT"):
        return False, "SIDE_INVALID", {}

    reserved = _reservation(state)
    if not reserved:
        return False, "CANONICAL_RESERVATION_MISSING", {}
    if int(reserved.get("opportunity_id", 0) or 0) != int(
        result.get("canonical_opportunity_id", 0) or 0
    ):
        return False, "CANONICAL_OPPORTUNITY_CHANGED", {}
    if str(reserved.get("causal_episode_id") or "") != str(
        result.get("causal_episode_id") or ""
    ):
        return False, "CAUSAL_EPISODE_CHANGED", {}

    ok, reason, authority_detail = _authority_contract(
        state, side, result, now,
    )
    if not ok:
        return ok, reason, authority_detail

    decision_ts = _f(result.get("ts"))
    proof_age = now - decision_ts
    if decision_ts <= 0.0 or proof_age < 0.0 or proof_age > PROOF_MAX_AGE_SECONDS:
        return False, "CAUSAL_PROOF_STALE", {"age_seconds": max(0.0, proof_age)}

    bid = _f(getattr(state, "execution_best_bid", 0.0))
    ask = _f(getattr(state, "execution_best_ask", 0.0))
    bbo_age = now - _f(getattr(state, "execution_price_time", 0.0))
    if bid <= 0.0 or ask <= bid:
        return False, "EXECUTION_BBO_INVALID", {"bid": bid, "ask": ask}
    if bbo_age < 0.0 or bbo_age > BBO_MAX_AGE_SECONDS:
        return False, "EXECUTION_BBO_STALE", {"age_seconds": max(0.0, bbo_age)}

    ok, reason, detail = _epoch_ok(state, result)
    if not ok:
        return ok, reason, detail
    rows = _rows_after(state, result)
    if rows is None:
        return False, "EXECUTED_FLOW_ENGINE_UNAVAILABLE", {}
    ok, reason, detail = _opposing_ok(rows, side)
    if not ok:
        return ok, reason, detail
    return True, "PASS", {
        **authority_detail,
        "proof_age_seconds": round(proof_age, 6),
        "bbo_age_seconds": round(bbo_age, 6),
        "post_result_rows": {name: len(value) for name, value in rows.items()},
    }


def _current_release(state, side, result, placed_at):
    """Require persistent cash release followed by an independent Futures response."""
    rows = _rows_after(state, result, cutoff_seconds=placed_at)
    if rows is None:
        return False, "EXECUTED_FLOW_ENGINE_UNAVAILABLE", {}
    side = str(side).upper()
    ignition = (result or {}).get("ignition") or {}
    for venue in ignition.get("cash_venues") or ():
        cash = _material_streak(rows.get(venue, ()), side)
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


def _current_consumed_fraction(state, side, result, placed_at, now):
    rows = _rows_after(state, result, cutoff_seconds=placed_at)
    if rows is None:
        return None, {"reason": "EXECUTED_FLOW_ENGINE_UNAVAILABLE"}
    ignition = (result or {}).get("ignition") or {}
    anchors = ignition.get("venue_anchor_prices") or {}
    sign = 1.0 if str(side).upper() == "LONG" else -1.0
    progress = max(0.0, _f(
        (ignition.get("phase_measurement") or {}).get(
            "precursor_cash_displacement_bps"
        )
    ))
    measured = False
    for venue in ignition.get("cash_venues") or ():
        current = rows.get(venue, ())
        anchor = _f(anchors.get(venue))
        price = _f(current[-1].get("price")) if current else 0.0
        if anchor <= 0.0 or price <= 0.0:
            continue
        measured = True
        progress = max(progress, max(0.0, sign * (price - anchor) / anchor * 10_000.0))
    spot = (
        _f(getattr(state, "best_bid", 0.0))
        + _f(getattr(state, "best_ask", 0.0))
    ) / 2.0
    atr = _f(getattr(state, "atr_1m", 0.0))
    atr_ts = _f(getattr(state, "atr_1m_updated_at", 0.0))
    atr_age = _f(now) - atr_ts
    atr_bps = atr / spot * 10_000.0 if spot > 0.0 and atr > 0.0 else 0.0
    if not measured or atr_bps <= 0.0 or atr_ts <= 0.0 or atr_age < -1.0 or atr_age > 120.0:
        return None, {
            "reason": "CURRENT_PHASE_SCALE_UNAVAILABLE",
            "cash_progress_measured": measured,
            "atr_age_seconds": max(0.0, atr_age) if atr_ts > 0.0 else None,
        }
    consumed = max(0.0, min(1.5, progress / atr_bps))
    return consumed, {
        "cash_displacement_bps": round(progress, 6),
        "phase_scale_bps": round(atr_bps, 6),
        "consumed_fraction": round(consumed, 6),
    }


def maker_ttl_release(state, side, result, now, placed_at):
    """Shadow-only maker fallback check for the same reserved causal episode."""
    ok, reason, detail = validate_submit(state, side, result, now)
    if not ok:
        return ok, reason, detail
    ok, reason, detail = _current_release(state, side, result, placed_at)
    if not ok:
        return ok, reason, detail
    consumed, phase_detail = _current_consumed_fraction(
        state, side, result, placed_at, now
    )
    if consumed is None:
        return False, "CURRENT_PHASE_SCALE_UNAVAILABLE", phase_detail
    if consumed > 0.35:
        return False, "CURRENT_IMPULSE_ALREADY_CONSUMED", phase_detail
    cost_ok, cost_reason, cost_detail = (
        verified_cost_model.validate_execution_cost_contract(
            result, state, "TAKER"
        )
    )
    if not cost_ok:
        return False, cost_reason, cost_detail
    return True, "CURRENT_RELEASE_PASS", {
        **detail,
        "current_phase": phase_detail,
        "current_execution_cost_bps": cost_detail["current_cost_bps"],
        "cost_budget_bps": cost_detail["budget_bps"],
        "cost_components": cost_detail["current"],
    }
