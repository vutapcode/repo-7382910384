#!/usr/bin/env python3
"""Fail closed when shadow snapshot and latest ENTRY/EXIT journal state disagree."""
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from loi_he_thong import journal_segments

ROOT = Path(
    os.environ.get("SMC_JOURNAL_DIR")
    or (Path.home() / ".local" / "state" / "smc2026" / "mainnet_shadow")
)
STATE_PATH = ROOT / "runtime_state.json"
EVENTS_PATH = Path(os.environ.get("SMC_SHADOW_EVENTS_PATH") or (ROOT / "events.jsonl"))
_RELEVANT = {"ENTRY", "EXIT"}


def _last_relevant_event(path, block_size=65536):
    return journal_segments.last_matching_event(
        path, _RELEVANT, block_size=block_size,
    )


def _fail(reason):
    print(f"[SHADOW-CONSISTENCY] FAIL {reason}", file=sys.stderr)
    raise SystemExit(1)


def _close(a, b, rel=1e-9, abs_tol=1e-12):
    a = float(a)
    b = float(b)
    return abs(a - b) <= max(abs_tol, rel * max(abs(a), abs(b), 1.0))


def validate(state_path=STATE_PATH, events_path=EVENTS_PATH):
    if not state_path.exists():
        return True
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        event = _last_relevant_event(events_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"read_error:{type(exc).__name__}:{exc}")

    pos = state.get("position")
    active = isinstance(pos, dict) and bool(pos.get("active", False))

    if event is None:
        if active:
            _fail("active_snapshot_without_entry_exit_journal")
        return True

    event_type = str(event.get("event", "")).upper()
    try:
        state_seq = int(state.get("event_seq", 0) or 0)
        event_seq = int(event.get("event_seq", 0) or 0)
    except (TypeError, ValueError):
        _fail("invalid_event_sequence")
    if state_seq < 0 or event_seq < 0:
        _fail("negative_event_sequence")
    if state_seq > 0 and event_seq > 0 and state_seq != event_seq:
        relation = "journal_ahead" if event_seq > state_seq else "snapshot_ahead"
        _fail(f"{relation}:state_seq={state_seq}:event_seq={event_seq}")

    if event_type == "EXIT":
        if active:
            _fail("active_snapshot_after_latest_exit")
        return True

    if event_type != "ENTRY":
        return True
    if not active:
        _fail("flat_snapshot_after_latest_entry")

    if str(pos.get("side", "")).upper() != str(event.get("side", "")).upper():
        _fail("entry_side_mismatch")
    if not _close(pos.get("entry_price", 0.0), event.get("price", 0.0)):
        _fail("entry_price_mismatch")
    if not _close(pos.get("qty", 0.0), event.get("qty_btc", 0.0)):
        _fail("entry_qty_mismatch")
    return True


if __name__ == "__main__":
    validate()
    print("[SHADOW-CONSISTENCY] OK")
