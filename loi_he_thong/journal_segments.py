"""Bounded, rotation-aware helpers for the active shadow event journal.

The current path remains ``events.jsonl``. Completed segments are immutable and
stay beside it long enough for every read-only tailer to drain its old inode.
This module owns storage mechanics only; it has no strategy authority.
"""
from __future__ import annotations

import os
import json
import threading
import time
from pathlib import Path


VERSION = "SHADOW_EVENT_JOURNAL_SEGMENTS_V1"
DEFAULT_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_SEGMENTS = 8
DEFAULT_RETENTION_SECONDS = 84 * 3600
_LOCK = threading.RLock()


def _identity(path):
    stat = Path(path).stat()
    return int(stat.st_dev), int(stat.st_ino)


def segment_glob(path):
    path = Path(path)
    return "%s.segment.*%s" % (path.stem, path.suffix)


def ordered_paths(current):
    """Return immutable segments oldest-first followed by the current file."""
    current = Path(current)
    rows = []
    for path in current.parent.glob(segment_glob(current)):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append((int(stat.st_mtime_ns), path.name, path))
    rows.sort()
    paths = [row[2] for row in rows]
    if current.exists():
        paths.append(current)
    return paths


def cursor_sources(current, device=0, inode=0, offset=0):
    """Plan lossless reads after a rename-based rotation.

    If the cursor inode is now an immutable segment, drain its unread tail and
    every newer segment before reading the current file. If the inode aged out,
    fail neutral by starting from the current file rather than inventing data.
    """
    current = Path(current)
    paths = ordered_paths(current)
    if not paths:
        return []
    wanted = (int(device or 0), int(inode or 0))
    if wanted != (0, 0):
        for index, path in enumerate(paths):
            try:
                if _identity(path) == wanted:
                    requested = max(0, int(offset or 0))
                    size = int(path.stat().st_size)
                    if path == current and requested > size:
                        # Same current inode was truncated/recreated in place.
                        # Its new contents have not been consumed.
                        start = 0
                    else:
                        # A renamed immutable inode cannot contain bytes beyond
                        # EOF. Replaying it from zero would duplicate gigabytes
                        # and can OOM every tailer; drain from its durable EOF
                        # and continue with newer segments/current instead.
                        start = min(requested, size)
                    return [
                        (candidate, start if pos == index else 0)
                        for pos, candidate in enumerate(paths[index:], index)
                    ]
            except OSError:
                continue
    return [(current, 0)] if current.exists() else []


def last_matching_event(current, event_names, block_size=65536):
    """Find the latest selected event across current and rotated segments."""
    wanted = {str(name).upper() for name in event_names}
    for path in reversed(ordered_paths(current)):
        try:
            size = path.stat().st_size
            if size <= 0:
                continue
            with path.open("rb") as handle:
                # A process kill can leave one unterminated final JSONL record.
                # It has no durable record boundary and must never acquire
                # authority. Scan only through the last newline; malformed
                # newline-terminated records still raise and fail closed.
                handle.seek(size - 1)
                if handle.read(1) == b"\n":
                    pos = size
                else:
                    search_end = size
                    pos = 0
                    while search_end > 0:
                        search_start = max(0, search_end - int(block_size))
                        handle.seek(search_start)
                        chunk = handle.read(search_end - search_start)
                        newline = chunk.rfind(b"\n")
                        if newline >= 0:
                            pos = search_start + newline + 1
                            break
                        search_end = search_start
                if pos <= 0:
                    continue
                carry = b""
                while pos > 0:
                    start = max(0, pos - int(block_size))
                    handle.seek(start)
                    data = handle.read(pos - start) + carry
                    lines = data.split(b"\n")
                    carry = lines[0]
                    for raw in reversed(lines[1:]):
                        if not raw.strip():
                            continue
                        row = json.loads(raw.decode("utf-8"))
                        if str(row.get("event") or "").upper() in wanted:
                            return row
                    pos = start
                if carry.strip():
                    row = json.loads(carry.decode("utf-8"))
                    if str(row.get("event") or "").upper() in wanted:
                        return row
        except FileNotFoundError:
            continue
    return None


def _fsync_parent(path):
    fd = os.open(str(Path(path).parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _segment_name(path, now_ns, inode):
    path = Path(path)
    return path.with_name(
        "%s.segment.%020d.%d%s" % (
            path.stem, int(now_ns), int(inode), path.suffix,
        )
    )


def _prune(path, now, retention_seconds, max_segments):
    segments = ordered_paths(path)
    if segments and segments[-1] == Path(path):
        segments = segments[:-1]
    survivors = []
    for candidate in segments:
        try:
            age = max(0.0, float(now) - candidate.stat().st_mtime)
        except OSError:
            continue
        if age > float(retention_seconds):
            try:
                candidate.unlink()
            except OSError:
                survivors.append(candidate)
        else:
            survivors.append(candidate)
    excess = max(0, len(survivors) - int(max_segments))
    for candidate in survivors[:excess]:
        try:
            candidate.unlink()
        except OSError:
            pass


def prepare_append(
    path, *, max_bytes=DEFAULT_MAX_BYTES,
    retention_seconds=DEFAULT_RETENTION_SECONDS,
    max_segments=DEFAULT_MAX_SEGMENTS, now=None,
):
    """Rotate before the next append when the current segment is full."""
    path = Path(path)
    # The production default is deliberately large, but accepting a smaller
    # explicit boundary keeps the storage contract testable and predictable.
    # Configuration validation belongs to the service layer; silently changing
    # the caller's requested boundary here makes rollover semantics ambiguous.
    max_bytes = max(1, int(max_bytes))
    now = time.time() if now is None else float(now)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stat = path.stat()
        except FileNotFoundError:
            path.touch(mode=0o600)
            return None
        if stat.st_size < max_bytes:
            return None
        target = _segment_name(path, time.time_ns(), stat.st_ino)
        os.replace(path, target)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        _fsync_parent(path)
        _prune(path, now, retention_seconds, max_segments)
        return target
