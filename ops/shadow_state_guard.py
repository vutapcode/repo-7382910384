#!/usr/bin/env python3
"""Fail-closed validator for persisted Mainnet shadow state."""
import json
import math
import os
import sys
from pathlib import Path

V1 = "SHADOW_RUNTIME_STATE_V1"
V2 = "SHADOW_RUNTIME_STATE_V2"
V3 = "SHADOW_RUNTIME_STATE_V3_PROMOTION_EVIDENCE"
V4 = "SHADOW_RUNTIME_STATE_V4_VERSION_BOUND_CALIBRATION"
V5 = "SHADOW_RUNTIME_STATE_V5_VERIFIED_COST_PLAN"
V6 = "SHADOW_RUNTIME_STATE_V6_ENTRY_ECONOMICS"
V7 = "SHADOW_RUNTIME_STATE_V7_ENTRY_ECONOMICS_V3"
V8 = "SHADOW_RUNTIME_STATE_V8_ENTRY_ECONOMICS_V4"
V9 = "SHADOW_RUNTIME_STATE_V9_ENTRY_ECONOMICS_V5"
V10 = "SHADOW_RUNTIME_STATE_V10_ENTRY_ECONOMICS_V6_AVAILABILITY_TIME"
V11 = "SHADOW_RUNTIME_STATE_V11_ENTRY_ECONOMICS_V7_CAUSAL_PROOF_SEMANTICS"
V12 = "SHADOW_RUNTIME_STATE_V12_ENTRY_ECONOMICS_V8_TIME_TO_EVENT"
V13 = "SHADOW_RUNTIME_STATE_V13_EXECUTION_PROTECTION_TRANSACTION"
V14 = "SHADOW_RUNTIME_STATE_V14_AUTHORITY_CONTRACTS"
VERSIONS = {V1, V2, V3, V4, V5, V6, V7, V8, V9, V10, V11, V12, V13, V14}
MODERN = {V2, V3, V4, V5, V6, V7, V8, V9, V10, V11, V12, V13, V14}
PROMOTION_STATE = {V3, V4, V5, V6, V7, V8, V9, V10, V11, V12, V13, V14}


def fail(msg):
    print(f"[SHADOW-STATE] FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def num(value, name, positive=False, nonnegative=False):
    try:
        out = float(value)
    except (TypeError, ValueError):
        fail(f"{name}:not_number")
    if not math.isfinite(out):
        fail(f"{name}:non_finite")
    if positive and out <= 0.0:
        fail(f"{name}:not_positive")
    if nonnegative and out < 0.0:
        fail(f"{name}:negative")
    return out


def counter(raw, name, optional=False):
    if optional and name not in raw:
        return 0
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        fail(f"{name}:invalid_counter")
    return value


root = Path(os.environ.get("SMC_JOURNAL_DIR") or
            (Path.home() / ".local" / "state" / "smc2026" / "mainnet_shadow"))
path = root / "runtime_state.json"
if not path.exists():
    print(f"[SHADOW-STATE] OK first_boot path={path}")
    raise SystemExit(0)

try:
    raw = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    fail(f"corrupt:{type(exc).__name__}:{exc}")
if not isinstance(raw, dict):
    fail("root:not_object")

version = raw.get("version")
if version not in VERSIONS:
    fail(f"unsupported_version:{version}")

required = {"balance", "realized_pnl", "trades", "wins", "losses", "position"}
missing = sorted(required.difference(raw))
if missing:
    fail("missing:" + ",".join(missing))

num(raw["balance"], "balance")
num(raw["realized_pnl"], "realized_pnl")
trades = counter(raw, "trades")
wins = counter(raw, "wins")
losses = counter(raw, "losses")
breakevens = counter(raw, "breakevens", optional=True)
event_seq = counter(raw, "event_seq", optional=True)
if version in MODERN and trades != wins + losses + breakevens:
    fail(f"counter_invariant:{trades}!={wins}+{losses}+{breakevens}")
if version in PROMOTION_STATE:
    evaluations = counter(raw, "decision_evaluations", optional=True)
    near_misses = counter(raw, "near_misses", optional=True)
    if near_misses > evaluations:
        fail(f"near_miss_invariant:{near_misses}>{evaluations}")
    funnel = raw.get("decision_funnel", {})
    if not isinstance(funnel, dict):
        fail("decision_funnel:not_object")
    funnel_total = 0
    for stage, value in funnel.items():
        if not isinstance(stage, str) or not stage:
            fail("decision_funnel:invalid_stage")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            fail(f"decision_funnel.{stage}:invalid_counter")
        funnel_total += value
    if funnel_total != evaluations:
        fail(f"decision_funnel_invariant:{funnel_total}!={evaluations}")
    opportunities = counter(raw, "canonical_opportunities", optional=True)
    consumed = counter(
        raw, "canonical_last_consumed_opportunity_id", optional=True
    )
    if consumed > opportunities:
        fail(f"canonical_consumed_invariant:{consumed}>{opportunities}")
    captured = counter(raw, "canonical_captured", optional=True)
    qualified = counter(raw, "canonical_qualified", optional=True)
    if captured > qualified:
        fail(f"canonical_capture_invariant:{captured}>{qualified}")
    if "shadow_day_start_ms" in raw:
        day_start = raw.get("shadow_day_start_ms")
        if isinstance(day_start, bool) or not isinstance(day_start, int) or day_start < 0:
            fail("shadow_day_start_ms:invalid")
    if "shadow_day_realized_pnl" in raw:
        num(raw.get("shadow_day_realized_pnl"), "shadow_day_realized_pnl")
    if "shadow_daily_locked" in raw and not isinstance(
        raw.get("shadow_daily_locked"), bool
    ):
        fail("shadow_daily_locked:not_bool")
    calibration_rows = raw.get("edge_calibration_rows", [])
    if not isinstance(calibration_rows, list) or len(calibration_rows) > 768:
        fail("edge_calibration_rows:invalid")
    for index, row in enumerate(calibration_rows):
        # Five fields are the legacy cohort; eight fields are the causal
        # cohort; nine fields append the frozen execution cost used for
        # current-cost repricing.
        if not isinstance(row, list) or len(row) not in (5, 8, 9):
            fail(f"edge_calibration_rows.{index}:invalid")
        bucket_fields = row[:-2] if len(row) == 9 else row[:-1]
        for field in bucket_fields:
            if not isinstance(field, str) or not field:
                fail(f"edge_calibration_rows.{index}:invalid_bucket")
        net_index = 7 if len(row) == 9 else -1
        num(row[net_index], f"edge_calibration_rows.{index}.net_bps")
        if len(row) == 9 and row[8] is not None:
            num(
                row[8], f"edge_calibration_rows.{index}.execution_cost_bps",
                nonnegative=True,
            )
    if version in {V4, V5, V6, V7, V8, V9, V10, V11, V12, V13, V14}:
        for name in (
            "edge_calibration_code_version",
            "edge_calibration_config_version",
        ):
            value = raw.get(name)
            if not isinstance(value, str) or not value:
                fail(f"{name}:missing")
    if version in {V6, V7, V8, V9, V10, V11, V12, V13, V14}:
        economics = raw.get("entry_economics_v2_rows")
        if not isinstance(economics, list) or len(economics) > 1024:
            fail("entry_economics_v2_rows:invalid")
        for name in (
            "entry_economics_code_version",
            "entry_economics_config_version",
        ):
            value = raw.get(name)
            if not isinstance(value, str) or not value:
                fail(f"{name}:missing")
        expected_contract = {
            V6: "ENTRY_ECONOMICS_V2",
            V7: "ENTRY_ECONOMICS_V3",
            V8: "ENTRY_ECONOMICS_V4",
            V9: "ENTRY_ECONOMICS_V5",
            V10: "ENTRY_ECONOMICS_V6_AVAILABILITY_TIME",
            V11: "ENTRY_ECONOMICS_V7_CAUSAL_PROOF_SEMANTICS",
            V12: "ENTRY_ECONOMICS_V8_TIME_TO_EVENT",
            V13: "ENTRY_ECONOMICS_V8_TIME_TO_EVENT",
            V14: "ENTRY_ECONOMICS_V8_TIME_TO_EVENT",
        }[version]
        for index, row in enumerate(economics):
            if not isinstance(row, dict) or row.get(
                "economic_contract_version"
            ) != expected_contract or row.get("valid") is not True:
                fail(f"entry_economics_v2_rows.{index}:invalid")
            num(
                row.get("net_pnl_bps_after_frozen_cost"),
                f"entry_economics_v2_rows.{index}.net_bps",
            )
            num(
                row.get("execution_cost_bps"),
                f"entry_economics_v2_rows.{index}.execution_cost_bps",
                nonnegative=True,
            )
            if version in {V12, V13, V14}:
                event = row.get("time_to_positive_net_event")
                if not isinstance(event, bool):
                    fail(f"entry_economics_v2_rows.{index}.event:invalid")
                termination = row.get("time_to_positive_net_termination")
                expected = (
                    "FIRST_POSITIVE_NET" if event
                    else "GUARDIAN_CLOSE_BEFORE_POSITIVE"
                )
                if termination != expected:
                    fail(f"entry_economics_v2_rows.{index}.termination:invalid")
                observed = row.get("time_to_positive_net_observation_seconds")
                if observed is not None:
                    num(
                        observed,
                        f"entry_economics_v2_rows.{index}.observation_seconds",
                        nonnegative=True,
                    )
                event_time = row.get("time_to_positive_net_seconds")
                if event and event_time is None:
                    fail(f"entry_economics_v2_rows.{index}.event_time:missing")
                if event_time is not None:
                    num(
                        event_time,
                        f"entry_economics_v2_rows.{index}.event_time",
                        nonnegative=True,
                    )

pos = raw.get("position")
if pos is not None:
    if not isinstance(pos, dict):
        fail("position:not_object")
    active = pos.get("active", True)
    if not isinstance(active, bool):
        fail("position.active:not_bool")
    if active:
        side = str(pos.get("side") or "").upper()
        if side not in {"LONG", "SHORT"}:
            fail(f"position.side:{side or 'missing'}")
        num(pos.get("qty"), "position.qty", positive=True)
        entry = num(pos.get("entry_price"), "position.entry_price", positive=True)
        if version in MODERN:
            r = num(pos.get("r"), "position.r", positive=True)
            hard_sl = num(pos.get("hard_sl"), "position.hard_sl", positive=True)
            best = num(pos.get("best"), "position.best", positive=True)
            best_r = num(pos.get("best_r"), "position.best_r", nonnegative=True)
            num(pos.get("fee_r"), "position.fee_r", nonnegative=True)

            if side == "LONG":
                if hard_sl >= entry:
                    fail("position.hard_sl:wrong_side_long")
                if best + 1e-9 < entry:
                    fail("position.best:below_entry_long")
            else:
                if hard_sl <= entry:
                    fail("position.hard_sl:wrong_side_short")
                if best - 1e-9 > entry:
                    fail("position.best:above_entry_short")

            expected_r = abs(entry - hard_sl)
            if expected_r <= 0.0 or abs(expected_r - r) / r > 0.05:
                fail("position.r:inconsistent_with_hard_sl")

            floor_r = pos.get("floor_r")
            floor_px = pos.get("floor")
            if floor_r is not None:
                floor_r = num(floor_r, "position.floor_r", nonnegative=True)
                if floor_r > best_r + 1e-6:
                    fail("position.floor_r:above_best_r")
            if floor_px is not None:
                floor_px = num(floor_px, "position.floor", positive=True)
                if side == "LONG" and floor_px > best + 1e-9:
                    fail("position.floor:above_best_long")
                if side == "SHORT" and floor_px < best - 1e-9:
                    fail("position.floor:below_best_short")

            cost_plan = pos.get("shadow_cost_plan")
            if version in {V5, V6, V7, V8, V9, V10, V11, V12, V13, V14} and cost_plan is not None:
                if not isinstance(cost_plan, dict):
                    fail("position.shadow_cost_plan:not_object")
                for name in (
                    "entry_fee_bps", "exit_fee_bps", "roundtrip_fee_bps",
                    "total_cost_bps",
                ):
                    num(
                        cost_plan.get(name),
                        f"position.shadow_cost_plan.{name}",
                        nonnegative=True,
                    )

transaction = raw.get("execution_transaction")
if version in {V13, V14}:
    if transaction is not None and not isinstance(transaction, dict):
        fail("execution_transaction:not_object")
    if isinstance(transaction, dict):
        if transaction.get("version") != "EXECUTION_PROTECTION_TRANSACTION_V1":
            fail("execution_transaction:unsupported_version")
        state_name = str(transaction.get("state") or "")
        allowed = {
            "INTENT_CREATED", "ORDER_SENT", "ACK_KNOWN", "EXECUTION_UNKNOWN",
            "PARTIAL_FILL_CONFIRMED", "FILL_CONFIRMED",
            "UNPROTECTED_EXPOSURE", "PROTECTION_SENT",
            "PROTECTION_ACKNOWLEDGED", "PROTECTION_VERIFICATION_FAILED",
            "PROTECTION_VERIFIED", "POSITION_PROTECTED",
            "EMERGENCY_FLATTEN_SENT", "RECOVERY_REQUIRED",
            "INVARIANT_BROKEN", "NO_POSITION", "FLAT_VERIFIED",
            "POSITION_CLOSED",
        }
        if state_name not in allowed:
            fail(f"execution_transaction.state:{state_name or 'missing'}")
        transitions = transaction.get("transitions")
        if not isinstance(transitions, list) or len(transitions) > 32:
            fail("execution_transaction.transitions:invalid")
        previous_sequence = 0
        for index, row in enumerate(transitions):
            if not isinstance(row, dict):
                fail(f"execution_transaction.transitions.{index}:not_object")
            row_state = str(row.get("state") or "")
            if row_state not in allowed:
                fail(
                    f"execution_transaction.transitions.{index}:invalid_state"
                )
            sequence = row.get("sequence")
            if (
                isinstance(sequence, bool) or not isinstance(sequence, int)
                or sequence <= previous_sequence
            ):
                fail(
                    f"execution_transaction.transitions.{index}:invalid_sequence"
                )
            previous_sequence = sequence
        if transitions and transitions[-1].get("state") != state_name:
            fail("execution_transaction.transitions:terminal_mismatch")
        if not isinstance(transaction.get("invariant_ok"), bool):
            fail("execution_transaction.invariant_ok:not_bool")
        for metric in (
            "decision_to_submit_ms", "submit_to_ack_ms",
            "fill_to_protection_submit_ms", "fill_to_protection_ack_ms",
            "fill_to_protection_verified_ms", "execution_unknown_duration_ms",
        ):
            if transaction.get(metric) is not None:
                num(
                    transaction.get(metric),
                    f"execution_transaction.{metric}", nonnegative=True,
                )
        if state_name == "POSITION_PROTECTED" and not (
            isinstance(pos, dict) and pos.get("active", True)
        ):
            fail("execution_transaction:protected_without_position")
    control = raw.get("execution_control_plane")
    if not isinstance(control, dict):
        fail("execution_control_plane:not_object")
    if control:
        if control.get("version") != "EXECUTION_CONTROL_PLANE_V1":
            fail("execution_control_plane:unsupported_version")
        health = str(control.get("health") or "")
        if health not in {
            "HEALTHY", "DEGRADED", "UNSAFE_FOR_NEW_ENTRY", "EXIT_ONLY",
            "UNKNOWN",
        }:
            fail("execution_control_plane.health:invalid")
        if not isinstance(control.get("entry_allowed"), bool):
            fail("execution_control_plane.entry_allowed:not_bool")

print(f"[SHADOW-STATE] OK version={version} trades={trades} w={wins} l={losses} be={breakevens} active={bool(pos and pos.get('active', True))}")
