"""One-time, flat-only virtual capital adjustment with durable audit event."""

import argparse
import json
import os
from pathlib import Path
import tempfile
import time


def last_position_event_seq(path, block_size=65536):
    """Return the durable ENTRY/EXIT sequence without parsing the full journal."""
    if not path.exists() or path.stat().st_size <= 0:
        return 0
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        carry = b""
        while position > 0:
            start = max(0, position - int(block_size))
            handle.seek(start)
            data = handle.read(position - start) + carry
            lines = data.split(b"\n")
            carry = lines[0]
            for encoded in reversed(lines[1:]):
                if not encoded.strip():
                    continue
                row = json.loads(encoded)
                if row.get("event") in {"ENTRY", "EXIT"}:
                    return int(row.get("event_seq", 0) or 0)
            position = start
        if carry.strip():
            row = json.loads(carry)
            if row.get("event") in {"ENTRY", "EXIT"}:
                return int(row.get("event_seq", 0) or 0)
    return 0


def atomic_json(path, payload):
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=float)
    parser.add_argument(
        "--state", type=Path,
        default=Path("/home/ubuntu/.local/state/smc2026/mainnet_shadow/runtime_state.json"),
    )
    parser.add_argument(
        "--journal", type=Path,
        default=Path("/home/ubuntu/.local/state/smc2026/mainnet_shadow/events.jsonl"),
    )
    parser.add_argument("--reason", default="USER_REQUESTED_VIRTUAL_CAPITAL_TOPUP")
    args = parser.parse_args()
    if args.target <= 0.0:
        raise SystemExit("target must be positive")
    raw = json.loads(args.state.read_text(encoding="utf-8"))
    position = raw.get("position")
    if isinstance(position, dict) and bool(position.get("active", True)):
        raise SystemExit("refusing balance adjustment while a position is active")
    old = float(raw.get("balance", 0.0) or 0.0)
    now = time.time()
    raw["balance"] = float(args.target)
    raw["ts"] = now
    # event_seq belongs exclusively to durable position ENTRY/EXIT. A capital
    # top-up is an audit event and must not advance that sequence.
    position_event_seq = last_position_event_seq(args.journal)
    if position_event_seq > 0:
        raw["event_seq"] = position_event_seq
    atomic_json(args.state, raw)
    atomic_json(args.state.with_suffix(args.state.suffix + ".bak"), raw)
    if abs(old - float(args.target)) <= 1e-12:
        print(json.dumps({
            "status": "NO_BALANCE_CHANGE",
            "balance_usdt": old,
            "position_event_seq": raw.get("event_seq"),
        }, ensure_ascii=False, indent=2))
        return
    event = {
        "ts": now,
        "runtime": "MAINNET_TIER_S_SHADOW_V1",
        "event": "SHADOW_CAPITAL_ADJUSTMENT",
        "schema_version": "TIER_S_SHADOW_CAPITAL_V1",
        "old_balance_usdt": old,
        "new_balance_usdt": float(args.target),
        "deposit_usdt": float(args.target) - old,
        "reason": args.reason,
        "lifetime_realized_pnl_preserved": raw.get("realized_pnl"),
        "lifetime_trades_preserved": raw.get("trades"),
        "position_event_seq_preserved": raw.get("event_seq"),
    }
    with args.journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(event, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
