#!/usr/bin/env python3
"""Fail closed if the shadow snapshot is missing while a non-empty journal already exists."""
import os
import sys
from pathlib import Path

root = Path(
    os.environ.get("SMC_JOURNAL_DIR")
    or (Path.home() / ".local" / "state" / "smc2026" / "mainnet_shadow")
)
state_path = root / "runtime_state.json"
events_path = Path(
    os.environ.get("SMC_SHADOW_EVENTS_PATH")
    or (root / "events.jsonl")
)

if not state_path.exists():
    try:
        journal_size = events_path.stat().st_size if events_path.exists() else 0
    except OSError as exc:
        print(
            f"[SHADOW-RECOVERY] FAIL journal_stat:{type(exc).__name__}:{exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if journal_size > 0:
        print(
            f"[SHADOW-RECOVERY] FAIL missing_state_with_existing_journal "
            f"state={state_path} journal={events_path} bytes={journal_size}",
            file=sys.stderr,
        )
        raise SystemExit(1)

print(
    f"[SHADOW-RECOVERY] OK state_exists={state_path.exists()} "
    f"journal_exists={events_path.exists()}"
)
