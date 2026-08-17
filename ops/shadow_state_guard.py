#!/usr/bin/env python3
"""Fail-closed validator for persisted Mainnet shadow state."""
import json
import math
import os
import sys
from pathlib import Path

VERSIONS = {"SHADOW_RUNTIME_STATE_V1", "SHADOW_RUNTIME_STATE_V2"}


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
if version == "SHADOW_RUNTIME_STATE_V2":
    required.add("breakevens")
missing = sorted(required.difference(raw))
if missing:
    fail("missing:" + ",".join(missing))

num(raw["balance"], "balance")
num(raw["realized_pnl"], "realized_pnl")
trades = counter(raw, "trades")
wins = counter(raw, "wins")
losses = counter(raw, "losses")
breakevens = counter(raw, "breakevens", optional=(version == "SHADOW_RUNTIME_STATE_V1"))
if version == "SHADOW_RUNTIME_STATE_V2" and trades != wins + losses + breakevens:
    fail(f"counter_invariant:{trades}!={wins}+{losses}+{breakevens}")

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
        if version == "SHADOW_RUNTIME_STATE_V2":
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

print(f"[SHADOW-STATE] OK version={version} trades={trades} w={wins} l={losses} be={breakevens} active={bool(pos and pos.get('active', True))}")
