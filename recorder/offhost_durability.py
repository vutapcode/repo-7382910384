"""Phase-7 immutable off-host durability primitives.

Research/preparation only. This module never reads the active append target,
never performs network I/O, and has no trading authority.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

VERSION = "OFFHOST_DURABILITY_MANIFEST_V1"
AUTHORITY = False
SANITIZED_RESEARCH_TELEMETRY_IS_WAL_BACKUP = False
SPOOL_STATES = frozenset({
    "PENDING", "UPLOADING", "ACKNOWLEDGED", "RETRYABLE_FAILURE",
    "PERMANENT_FAILURE", "CORRUPT",
})
SECRET_MARKERS = ("AUTHORIZATION", "API_KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY")


class DurabilityError(ValueError):
    pass


def _utc_iso_from_seconds(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def _canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _safe_relative(path: Path, root: Path) -> str:
    root = root.resolve(strict=True)
    path = path.resolve(strict=True)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise DurabilityError("ARTIFACT_OUTSIDE_DATA_ROOT") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise DurabilityError("ARTIFACT_PATH_INVALID")
    return relative.as_posix()


def _reject_symlink_chain(path: Path, root: Path) -> None:
    root = root.resolve(strict=True)
    candidate = path
    while True:
        if candidate.is_symlink():
            raise DurabilityError("SYMLINK_ARTIFACT_REJECTED")
        if candidate == root:
            return
        if root not in candidate.parents:
            raise DurabilityError("ARTIFACT_OUTSIDE_DATA_ROOT")
        candidate = candidate.parent


def _wal_partition(path: Path, root: Path):
    base = root / "raw" / "wal"
    try:
        rel = path.relative_to(base)
    except ValueError:
        return None
    if len(rel.parts) != 3 or path.suffix != ".jsonl":
        return None
    stream, date, filename = rel.parts
    try:
        hour = datetime.strptime(f"{date}/{Path(filename).stem}", "%Y-%m-%d/%H").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return stream, hour


def immutable_artifact_kind(path, data_root, now=None):
    """Return (kind, stream, utc_partition) only for closed immutable inputs."""
    path = Path(path)
    root = Path(data_root).resolve(strict=True)
    _reject_symlink_chain(path, root)
    resolved = path.resolve(strict=True)
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    wal = _wal_partition(resolved, root)
    if wal is not None:
        stream, hour = wal
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        if hour >= current_hour:
            raise DurabilityError("CURRENT_OPEN_WAL_NOT_SEALABLE")
        return "CLOSED_WAL", stream, hour.strftime("%Y-%m-%d/%H")
    if ".segment." in resolved.name and resolved.suffix == ".jsonl":
        return "CLOSED_JOURNAL_SEGMENT", "shadow_event_journal", datetime.fromtimestamp(
            resolved.stat().st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%d/%H")
    raise DurabilityError("ARTIFACT_NOT_CLOSED_IMMUTABLE_SEGMENT")


def _availability_ms(record):
    for name in ("available_time_ms", "receive_time_ms", "event_time_ms"):
        value = record.get(name)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    try:
        return int(float(record.get("ts")) * 1000.0)
    except (TypeError, ValueError):
        return None


def _one_or_mixed(values):
    values = sorted({str(value) for value in values if value not in (None, "")})
    if not values:
        return "UNKNOWN"
    return values[0] if len(values) == 1 else "MIXED:" + hashlib.sha256("\0".join(values).encode()).hexdigest()[:16]


def scan_jsonl(path, chunk_size=1024 * 1024):
    """Streaming checksum plus bounded JSONL metadata scan."""
    path = Path(path)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(int(chunk_size))
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)

    rows = 0
    first_available = None
    last_available = None
    versions = {name: set() for name in (
        "schema_version", "code_version", "config_version", "event_contract_version",
    )}
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            rows += 1
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise DurabilityError("WAL_ROW_INVALID_JSON") from exc
            available = _availability_ms(record)
            if available is not None:
                first_available = available if first_available is None else min(first_available, available)
                last_available = available if last_available is None else max(last_available, available)
            for name in versions:
                versions[name].add(record.get(name))
    return {
        "byte_size": size,
        "sha256": digest.hexdigest(),
        "row_count": rows,
        "first_availability_time_ms": first_available,
        "last_availability_time_ms": last_available,
        "schema_version": _one_or_mixed(versions["schema_version"]),
        "code_version": _one_or_mixed(versions["code_version"]),
        "config_version": _one_or_mixed(versions["config_version"]),
        "event_contract_version": _one_or_mixed(versions["event_contract_version"]),
    }


def build_manifest(path, data_root, *, now=None, wal_schema_version=None, canonical_replay_hash=None):
    path = Path(path)
    root = Path(data_root)
    kind, stream, partition = immutable_artifact_kind(path, root, now=now)
    relative = _safe_relative(path, root)
    scan = scan_jsonl(path)
    stable = {
        "durability_schema_version": VERSION,
        "artifact_kind": kind,
        "source_relative_path": relative,
        "stream": stream,
        "utc_partition": partition,
        "byte_size": scan["byte_size"],
        "sha256": scan["sha256"],
        "wal_schema_version": str(wal_schema_version or scan["schema_version"]),
        "schema_version": scan["schema_version"],
        "code_version": scan["code_version"],
        "config_version": scan["config_version"],
        "event_contract_version": scan["event_contract_version"],
        "first_availability_time_ms": scan["first_availability_time_ms"],
        "last_availability_time_ms": scan["last_availability_time_ms"],
        "row_count": scan["row_count"],
        "canonical_replay_hash": canonical_replay_hash,
    }
    artifact_id = hashlib.sha256(_canonical_json(stable)).hexdigest()
    stamp = _utc_iso_from_seconds(path.stat().st_mtime)
    manifest = {
        **stable,
        "artifact_id": artifact_id,
        "created_at_utc": stamp,
        "sealed_at_utc": stamp,
        "authority": False,
        "backup_semantics": "IMMUTABLE_WAL_COPY_REQUIRED",
    }
    text = _canonical_json(manifest).decode("utf-8").upper()
    if any(marker in text for marker in SECRET_MARKERS):
        raise DurabilityError("MANIFEST_SECRET_MARKER_REJECTED")
    return manifest


def atomic_write_manifest(path, manifest):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if current != manifest:
            raise DurabilityError("IMMUTABLE_MANIFEST_CONFLICT")
        return path
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        try:
            os.link(temp_name, path)
        except FileExistsError:
            current = json.loads(path.read_text(encoding="utf-8"))
            if current != manifest:
                raise DurabilityError("IMMUTABLE_MANIFEST_CONFLICT")
        return path
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class SpoolRecord:
    artifact_id: str
    artifact_path: str
    manifest_path: str
    byte_size: int
    state: str = "PENDING"
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str | None = None

    def __post_init__(self):
        if self.state not in SPOOL_STATES:
            raise DurabilityError("INVALID_SPOOL_STATE")

    def to_dict(self):
        return asdict(self)
