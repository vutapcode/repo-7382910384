#!/usr/bin/env python3
"""Fail closed when a stateful journal exists without its shadow snapshot."""
import json
import os
import sys
from pathlib import Path


STATE_REQUIRING_EVENTS = frozenset({
    "ENTRY", "EXIT", "LIVE_ENTRY", "LIVE_EXIT", "LIVE_EXCHANGE_EXIT",
    "AUTO_PROMOTED", "DIRECT_LIVE_ARMED", "AUTO_DEMOTED",
})


def journal_requires_state(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"journal_corrupt_line:{number}:{type(exc).__name__}"
                    ) from exc
                if str((row or {}).get("event", "")) in STATE_REQUIRING_EVENTS:
                    return True
        return False
    except OSError as exc:
        raise RuntimeError(f"journal_read:{type(exc).__name__}:{exc}") from exc

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

    try:
        requires_state = bool(journal_size and journal_requires_state(events_path))
    except RuntimeError as exc:
        print(f"[SHADOW-RECOVERY] FAIL {exc}", file=sys.stderr)
        raise SystemExit(1)

    if requires_state:
        print(
            f"[SHADOW-RECOVERY] FAIL missing_state_with_stateful_journal "
            f"state={state_path} journal={events_path} bytes={journal_size}",
            file=sys.stderr,
        )
        raise SystemExit(1)

print(
    f"[SHADOW-RECOVERY] OK state_exists={state_path.exists()} "
    f"journal_exists={events_path.exists()}"
)
