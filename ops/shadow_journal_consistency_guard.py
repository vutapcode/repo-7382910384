#!/usr/bin/env python3
"""Fail closed when shadow snapshot and latest ENTRY/EXIT journal state disagree."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(
    os.environ.get("SMC_JOURNAL_DIR")
    or (Path.home() / ".local" / "state" / "smc2026" / "mainnet_shadow")
)
STATE_PATH = ROOT / "runtime_state.json"
EVENTS_PATH = Path(os.environ.get("SMC_SHADOW_EVENTS_PATH") or (ROOT / "events.jsonl"))
_RELEVANT = {"ENTRY", "EXIT"}


def _last_relevant_event(path, block_size=65536):
    if not path.exists() or path.stat().st_size <= 0:
        return None
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        pos = handle.tell()
        carry = b""
        while pos > 0:
            start = max(0, pos - int(block_size))
            handle.seek(start)
            chunk = handle.read(pos - start)
            data = chunk + carry
            lines = data.split(b"\n")
            carry = lines[0]
            for raw in reversed(lines[1:]):
                raw = raw.strip()
                if not raw:
                    continue
                row = json.loads(raw.decode("utf-8"))
                if row.get("event") in _RELEVANT:
                    return row
            pos = start
        raw = carry.strip()
        if raw:
            row = json.loads(raw.decode("utf-8"))
            if row.get("event") in _RELEVANT:
                return row
    return None


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
