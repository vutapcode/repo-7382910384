#!/usr/bin/env python3
"""Fail closed if persisted Mainnet shadow state is corrupt or impossible."""
import json, math, os, sys
from pathlib import Path

VERSIONS = {"SHADOW_RUNTIME_STATE_V1", "SHADOW_RUNTIME_STATE_V2"}

def die(msg):
    print(f"[SHADOW-STATE] FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)

root = Path(os.environ.get("SMC_JOURNAL_DIR") or (Path.home()/".local"/"state"/"smc2026"/"mainnet_shadow"))
path = root/"runtime_state.json"
if not path.exists():
    print(f"[SHADOW-STATE] OK first_boot path={path}")
    raise SystemExit(0)

try:
    raw = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    die(f"corrupt:{type(exc).__name__}:{exc}")
if not isinstance(raw, dict):
    die("root_not_object")
version = raw.get("version")
if version not in VERSIONS:
    die(f"unsupported_version:{version}")

required = {"balance","realized_pnl","trades","wins","losses","position"}
if version == "SHADOW_RUNTIME_STATE_V2":
    required.add("breakevens")
missing = sorted(required.difference(raw))
if missing:
    die("missing:" + ",".join(missing))

for name in ("balance","realized_pnl"):
    try:
        value = float(raw[name])
    except Exception:
        die(f"{name}:not_number")
    if not math.isfinite(value):
        die(f"{name}:non_finite")

def counter(name, optional=False):
    if optional and name not in raw:
        return 0
    v = raw.get(name)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        die(f"{name}:invalid_counter")
    return v

trades, wins, losses = counter("trades"), counter("wins"), counter("losses")
be = counter("breakevens", optional=(version == "SHADOW_RUNTIME_STATE_V1"))
if version == "SHADOW_RUNTIME_STATE_V2" and trades != wins + losses + be:
    die(f"counter_invariant:{trades}!={wins}+{losses}+{be}")

pos = raw.get("position")
if pos is not None:
    if not isinstance(pos, dict):
        die("position:not_object")
    if pos.get("active", True):
        if str(pos.get("side") or "").upper() not in {"LONG","SHORT"}:
            die("position:bad_side")
        keys = ["qty","entry_price"] + (["r","hard_sl"] if version == "SHADOW_RUNTIME_STATE_V2" else [])
        for name in keys:
            try:
                v = float(pos.get(name))
            except Exception:
                die(f"position.{name}:not_number")
            if not math.isfinite(v) or v <= 0:
                die(f"position.{name}:not_positive")

print(f"[SHADOW-STATE] OK version={version} trades={trades} w={wins} l={losses} be={be} active={bool(pos and pos.get('active', True))}")
