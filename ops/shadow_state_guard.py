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
VERSIONS = {V1, V2, V3, V4, V5}


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
if version in {V2, V3, V4, V5} and trades != wins + losses + breakevens:
    fail(f"counter_invariant:{trades}!={wins}+{losses}+{breakevens}")
if version in {V3, V4, V5}:
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
    if version in {V4, V5}:
        for name in (
            "edge_calibration_code_version",
            "edge_calibration_config_version",
        ):
            value = raw.get(name)
            if not isinstance(value, str) or not value:
                fail(f"{name}:missing")

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
        if version in {V2, V3, V4, V5}:
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
            if version == V5 and cost_plan is not None:
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

print(f"[SHADOW-STATE] OK version={version} trades={trades} w={wins} l={losses} be={breakevens} active={bool(pos and pos.get('active', True))}")
