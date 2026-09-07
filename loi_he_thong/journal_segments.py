"""Bounded, rotation-aware helpers for the active shadow event journal.

The current path remains ``events.jsonl``. Completed segments are immutable and
stay beside it long enough for every read-only tailer to drain its old inode.
This module owns storage mechanics only; it has no strategy authority.
"""
from __future__ import annotations

import os
import json
import tempfile
import threading
import time
from pathlib import Path


VERSION = "SHADOW_EVENT_JOURNAL_SEGMENTS_V1"
CRITICAL_CURSOR_VERSION = "CRITICAL_EVENT_CURSOR_V1"
DEFAULT_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_SEGMENTS = 8
DEFAULT_RETENTION_SECONDS = 84 * 3600
_LOCK = threading.RLock()


def _discard_scan_cache(handle, start, length):
    """Do not let a cold safety scan evict the live runtime working set."""
    advise = getattr(os, "posix_fadvise", None)
    dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
    if advise is None or dontneed is None or int(length) <= 0:
        return
    try:
        advise(handle.fileno(), int(start), int(length), dontneed)
    except OSError:
        # This is only a page-cache hint. Journal parsing remains fail-closed.
        pass


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
                    read_length = pos - start
                    data = handle.read(read_length) + carry
                    lines = data.split(b"\n")
                    carry = lines[0]
                    for raw in reversed(lines[1:]):
                        if not raw.strip():
                            continue
                        row = json.loads(raw.decode("utf-8"))
                        if str(row.get("event") or "").upper() in wanted:
                            return row
                    # Reverse-scanning up to the full retained journal can be
                    # correct but must not pin ~1GiB of cold pages inside the
                    # bot's MemoryHigh cgroup during startup.
                    data = None
                    lines = None
                    _discard_scan_cache(handle, start, read_length)
                    pos = start
                if carry.strip():
                    row = json.loads(carry.decode("utf-8"))
                    if str(row.get("event") or "").upper() in wanted:
                        return row
        except FileNotFoundError:
            continue
    return None


def _complete_boundary(path, block_size=65536):
    """Return the byte after the last complete JSONL record."""
    path = Path(path)
    size = path.stat().st_size
    if size <= 0:
        return 0
    with path.open("rb") as handle:
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            return size
        end = size
        while end > 0:
            start = max(0, end - int(block_size))
            handle.seek(start)
            chunk = handle.read(end - start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            end = start
    return 0


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_matching_cursor(current, cursor_path, event_names, latest_event):
    """Persist an optimization cursor; the journal remains the authority."""
    current = Path(current)
    if not current.exists():
        return
    stat = current.stat()
    _atomic_json(cursor_path, {
        "version": CRITICAL_CURSOR_VERSION,
        "event_names": sorted(str(name).upper() for name in event_names),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "offset": _complete_boundary(current),
        "latest_event": latest_event,
    })


def _load_matching_cursor(current, cursor_path, event_names):
    try:
        payload = json.loads(Path(cursor_path).read_text(encoding="utf-8"))
        if payload.get("version") != CRITICAL_CURSOR_VERSION:
            return None
        expected = sorted(str(name).upper() for name in event_names)
        if payload.get("event_names") != expected:
            return None
        wanted = (int(payload["device"]), int(payload["inode"]))
        offset = int(payload["offset"])
        if offset < 0:
            return None
        source = None
        for candidate in ordered_paths(current):
            if _identity(candidate) == wanted:
                source = candidate
                break
        if source is None or offset > source.stat().st_size:
            return None
        latest = payload.get("latest_event")
        if latest is not None and not isinstance(latest, dict):
            return None
        return latest, wanted, offset
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def last_matching_event_cached(
    current, event_names, cursor_path, block_size=65536,
):
    """Find the latest selected event, incrementally after the first scan.

    The cursor can only reduce work: missing, stale, truncated, rotated-out or
    corrupt cursors fall back to the canonical reverse journal scan.
    """
    current = Path(current)
    wanted = {str(name).upper() for name in event_names}
    cached = _load_matching_cursor(current, cursor_path, wanted)
    if cached is None:
        latest = last_matching_event(current, wanted, block_size=block_size)
        write_matching_cursor(current, cursor_path, wanted, latest)
        return latest

    latest, identity, offset = cached
    sources = cursor_sources(current, identity[0], identity[1], offset)
    for source, start in sources:
        try:
            with source.open("rb") as handle:
                handle.seek(int(start))
                scan_start = int(start)
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        break
                    row = json.loads(line.decode("utf-8"))
                    if str(row.get("event") or "").upper() in wanted:
                        latest = row
                _discard_scan_cache(
                    handle, scan_start, max(0, handle.tell() - scan_start),
                )
        except FileNotFoundError:
            # Rotation pruning between planning and opening cannot grant
            # authority. Rebuild from the retained canonical journal.
            latest = last_matching_event(current, wanted, block_size=block_size)
            break
    write_matching_cursor(current, cursor_path, wanted, latest)
    return latest


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
