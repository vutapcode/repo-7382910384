"""Minimal fail-closed WStrade Mainnet executor.

Only intents that already passed the active Tier-S councils and immutable edge
gates reach this module. One fixed 0.001 BTC position is allowed. Every fill
must acquire a verified exchange hard stop or it is immediately flattened.
"""

import asyncio
import hashlib
import math
import os
from types import SimpleNamespace
import time

from loi_he_thong import ignition_signals
from loi_he_thong import mainnet_safety
from loi_he_thong import private_user_stream
from loi_he_thong import verified_cost_model


VERSION = "WSTRADE_LIVE_EXECUTION_V1"
SYMBOL = "BTCUSDT"
MAKER_TTL_SECONDS = 0.75
CAUSAL_SUBMIT_MAX_AGE_SECONDS = 1.5
BBO_SUBMIT_MAX_AGE_SECONDS = 1.0


def _execution_flow_engine(state):
    value = getattr(state, "_ignition_signal_engine", None)
    return value if isinstance(value, ignition_signals.SignalEngine) else None


def _post_decision_rows(state, result, cutoff_seconds=None):
    """Read finalized receive-time buckets only; never finalize/evaluate strategy."""
    signal_engine = _execution_flow_engine(state)
    if signal_engine is None:
        return None
    cutoff = max(
        float((result or {}).get("ts", 0.0) or 0.0),
        float(cutoff_seconds or 0.0),
    )
    cutoff_ms = int(cutoff * 1000.0)
    return {
        name: tuple(
            row for row in venue.history
            if int(row.get("receive_time_ms", 0) or 0) > cutoff_ms
        )
        for name, venue in signal_engine.venues.items()
    }


def _flow_deterioration(row):
    venue = str((row or {}).get("renue") or "")
    return bool(
        venue in ignition_signals.MIN_QTY
        and float(row.get("total_qty", 0.0) or 0.0) >= ignition_signals.MIN_QTY[venue]
        and abs(float(row.get("imbalance", 0.0) or 0.0)) >= 0.20
        and bool(row.get("clock_valid"))
    )


def _two_strong_buckets(rows, side, opposing=False):
    side = str(side or "").upper()
    previous_bucket = None
    for row in rows:
        row_side = str(row.get("side") or "").upper()
        matched = row_side != side if opposing else row_side == side
        if row_side not in ("LONG", "SHORT") or not matched or not bool(row.get("strong")):
            previous_bucket = None
            continue
        bucket = int(row.get("bucket_start_ms", 0) or 0)
        if (
            previous_bucket is not None
            and bucket - previous_bucket == ignition_signals.BUCKET_MS
        ):
            return True
        previous_bucket = bucket
    return False


def _causal_continuity_ok(state, side, result):
    """Pure read-only epoch/gap/new-flow check folded into submit revalidation."""
    result = result or {}
    opportunity_id = int(result.get("canonical_opportunity_id", 0) or 0)
    if opportunity_id <= 0:
        return True, "PASS"

    reserved = getattr(state, "canonical_reserved_context", None)
    if not isinstance(reserved, dict):
        return False, "CANONICAL_RESERVATION_MISSING"
    if int(reserved.get("opportunity_id", 0) or 0) != opportunity_id:
        return False, "CANONICAL_OPPORTUNITY_CHANGED"
    if str(reserved.get("causal_episode_id") or "") != str(result.get("causal_episode_id") or ""):
        return False, "CAUSAL_EPISODE_CHANGED"
    if bool(getattr(state, "shadow_data_gap_active", False)):
        return False, "EXECUTED_FLOW_GAP_ACTIVE"

    signal_engine = _execution_flow_engine(state)
    if signal_engine is None:
        return False, "EXECUTED_FLOW_ENGINE_UNAVAILABLE"
    ignition = result.get("ignition") or {}
    expected_epochs = dict(reserved.get("epochs") or {})
    if not expected_epochs:
        expected_epochs = {
            str(name): int((row or {}).get("epoch", 0) or 0)
            for name, row in (ignition.get("clock_quality") or {}).items()
        }
    required = set(ignition.get("cash_venues") or ())
    required.add("futures")
    for name in sorted(required):
        venue = signal_engine.venues.get(name)
        if venue is None:
            return False, "EXECUTED_FLOW_VENUE_UNAVAILABLE"
        if not bool(venue.clock_valid):
            return False, "EXECUTED_FLOW_CLOCK_INVALID"
        if name in expected_epochs and int(venue.epoch) != int(expected_epochs[name]):
            return False, "EXECUTED_FLOW_EPOCH_RESET"

    rows = _post_decision_rows(state, result)
    if rows is None:
        return False, "EXECUTED_FLOW_ENGINE_UNAVAILABLE"
    side = str(side).upper()

    # One cash jerk cannot veto. Require two material 100 ms buckets, or
    # independent price deterioration plus opposing executed-flow deterioration.
    for name in ignition.get("cash_venues") or ():
        venue_rows = rows.get(name, ())
        if _two_strong_buckets(venue_rows, side, opposing=True):
            return False, "POST_PROOF_CASH_REVERSAL_2_BUCKETS"
        price_row = next((
            row for row in reversed(venue_rows)
            if side == "LONG" and float(row.get("price_conversion_bps", 0.0) or 0.0) <= -0.15
            or side == "SHORT" and float(row.get("price_conversion_bps", 0.0) or 0.0) >= 0.15
        ), None)
        if price_row is not None:
            price_bucket = int(price_row.get("bucket_start_ms", 0) or 0)
            if any(
                int(row.get("bucket_start_ms", 0) or 0) != price_bucket
                and str(row.get("side") or "").upper() in ("LONG", "SHORT")
                and str(row.get("side") or "").upper() != side
                and _flow_deterioration(row)
                for row in venue_rows
            ):
                return False, "POST_PROOF_CASH_PRICE_FLOW_REVERSAL"

    # Futures is noisier: one isolated material bucket is explicitly tolerated.
    if _two_strong_buckets(rows.get("futures", ()), side, opposing=True):
        return False, "POST_PROOF_FUTURES_REVERSAL_2_BUCKETS"
    return True, "PASS"


def _shadow_maker_release_ok(state, side, result, placed_at, now):
    """Shadow-only current RELEASE check for maker TTL; no new episode/evaluate."""
    rows = _post_decision_rows(state, result, cutoff_seconds=placed_at)
    if rows is None:
        return False, "EXECUTED_FLOW_ENGINE_UNAVAILABLE"
    ignition = (result or {}).get("ignition") or {}

    cash_release_ms = None
    for name in ignition.get("cash_venues") or ():
        venue_rows = rows.get(name, ())
        previous = None
        for row in venue_rows:
            if (
                str(row.get("side") or "").upper() != str(side).upper()
                or not bool(row.get("strong"))
            ):
                previous = None
                continue
            if (
                previous is not None
                and int(row.get("bucket_start_ms", 0) or 0)
                    - int(previous.get("bucket_start_ms", 0) or 0)
                    == ignition_signals.BUCKET_MS
                and float(row.get("flow_acceleration", 0.0) or 0.0) >= 0.0
            ):
                cash_release_ms = int(row.get("receive_time_ms", 0) or 0)
                break
            previous = row
        if cash_release_ms is not None:
            break
    if cash_release_ms is None:
        return False, "MAKER_CURRENT_CASH_RELEASE_NOT_PROVED"

    futures_response = any(
        str(row.get("side") or "").upper() == str(side).upper()
        and bool(row.get("strong"))
        and 0 <= int(row.get("receive_time_ms", 0) or 0) - cash_release_ms <= 600
        for row in rows.get("futures", ())
    )
    if not futures_response:
        return False, "MAKER_CURRENT_FUTURES_RESPONSE_MISSING"

    # Reuse the existing consumed<=0.35 rule; do not invent a chase threshold.
    spot = (
        float(getattr(state, "best_bid", 0.0) or 0.0)
        + float(getattr(state, "best_ask", 0.0) or 0.0)
    ) / 2.0
    atr = float(getattr(state, "atr_1m", 0.0) or 0.0)
    atr_ts = float(getattr(state, "atr_1m_updated_at", 0.0) or 0.0)
    atr_age = float(now) - atr_ts
    atr_bps = atr / spot * 10000.0 if spot > 0.0 and atr > 0.0 else 0.0
    if atr_bps <= 0.0 or atr_ts <= 0.0 or atr_age < -1.0 or atr_age > 120.0:
        return False, "MAKER_CURRENT_ATR_UNAVAILABLE"

    sign = 1.0 if str(side).upper() == "LONG" else -1.0
    anchors = ignition.get("venue_anchor_prices") or {}
    progress = float(
        (ignition.get("phase_measurement") or {}).get(
            "precursor_cash_displacement_bps", 0.0
        ) or 0.0
    )
    for name in ignition.get("cash_venues") or ():
        venue_rows = rows.get(name, ())
        if not venue_rows:
            continue
        anchor = float(anchors.get(name, 0.0) or 0.0)
        price = float(venue_rows[-1].get("price", 0.0) or 0.0)
        if anchor > 0.0 and price > 0.0:
            progress = max(progress, max(0.0, sign * (price - anchor) / anchor * 10000.0))
    if max(0.0, progress) / atr_bps > 0.35:
        return False, "MAKER_CURRENT_IMPULSE_ALREADY_CONSUMED"

    current = dict(result or {})
    current["phase"] = "RELEASE"
    current["execution_policy"] = "TAKER"
    costs = verified_cost_model.estimate(current, state)
    try:
        total_cost = float(costs.get("total_cost_bps"))
    except (TypeError, ValueError):
        total_cost = -1.0
    if total_cost < 0.0 or not bool(costs.get("commission_verified")):
        return False, "MAKER_CURRENT_COST_UNVERIFIED"
    state.mainnet_shadow_maker_current_cost = dict(costs)
    return True, "PASS"


def _revalidate_before_submit(
    state, side, result, now=None, shadow_maker_placed_at=None
):
    """Fail closed if REST preflight outlived the recorded causal decision."""
    now = time.time() if now is None else float(now)
    side = str(side or "").upper()
    if side not in ("LONG", "SHORT"):
        return False, "SIDE_INVALID"
    if str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper() != side:
        return False, "BIAS_SIDE_CHANGED"
    if float(getattr(state, "bias_confidence", 0.0) or 0.0) < 0.55:
        return False, "BIAS_CONFIDENCE_DROPPED"
    decision_ts = float((result or {}).get("sts", 0.0) or 0.0)
    decision_age = now - decision_ts
    if (
        decision_ts <= 0.0 or decision_age < 0.0
        or decision_age > CAUSAL_SUBMIT_MAX_AGE_SECONDS
    ):
        return False, "CAUSAL_PROOF_STALE"
    bid = float(getattr(state, "execution_best_bid", 0.0) or 0.0)
    ask = float(getattr(state, "execution_best_ask", 0.0) or 0.0)
    bbo_ts = float(getattr(state, "execution_price_time", 0.0) or 0.0)
    bbo_age = now - bbo_ts
    if bid <= 0.0 or ask <= bid:
        return False, "BBO_INVALID"
    if bbo_ts <= 0.0 or bbo_age < 0.0 or bbo_age > BBO_SUBMIT_MAX_AGE_SECONDS:
        return False, "BBO_STALE"
    ignition = (result or {}).get("ignition") or {}
    if ignition.get("futures_follow_invalidated"):
        return False, "FUTURES_FOLLOW_INVALIDATED"
    ok, reason = _causal_continuity_ok(state, side, result)
    if not ok:
        return ok, reason
    if shadow_maker_placed_at is not None:
        return _shadow_maker_release_ok(
            state, side, result, float(shadow_maker_placed_at), now
        )
    return True, "PASS"


def _finalize_shadow_state(state):
    state.wstrade_live_armed = False
    state.wstrade_live_entry_allowed = False
    state.execution_allowed = False
    state.trading_enabled = False
    state.mainnet_shadow = True
    state.mainnet_shadow_real_orders_blocked = True
    state.execution_venue = "BINANCE_FUTURES_MAINNET_SHADOW"
    for name in (
        "SMC_MAINNET_ARMED", "SMC_MAINNET_EXCLUSIVE_ACCOUNT", "SMC_ENABLE_TRADING"
    ):
        os.environ[name] = "false"


# ... rest of file preserved ...
