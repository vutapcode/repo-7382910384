"""Disabled-by-default asynchronous spool for immutable off-host artifacts."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import time

from recorder.offhost_durability import SpoolRecord

VERSION = "OFFHOST_SPOOL_V1"
AUTHORITY = False
DEFAULT_ENABLED = False


class OffhostSpool:
    def __init__(self, root, *, max_items=256, max_bytes=8 * 1024**3, retry_base=1.0, retry_cap=300.0, warn_free_bytes=512 * 1024**2):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "spool-index-v1.json"
        self.max_items = int(max_items); self.max_bytes = int(max_bytes)
        self.retry_base = float(retry_base); self.retry_cap = float(retry_cap); self.warn_free_bytes = int(warn_free_bytes)
        self.records = {}
        self.last_success_at = None; self.last_error = None; self.checksum_failures = 0
        self.alarm = None
        self._load_index()

    def _load_index(self):
        if not self.index_path.exists():
            return
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            rows = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("SPOOL_INDEX_RECORDS_INVALID")
            loaded = {}
            for row in rows:
                record = SpoolRecord(**dict(row or {}))
                if record.artifact_id in loaded:
                    raise ValueError("SPOOL_INDEX_DUPLICATE_ARTIFACT")
                loaded[record.artifact_id] = record
            self.records = loaded
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.records = {}
            self.last_error = f"SPOOL_INDEX_INVALID:{type(exc).__name__}"
            self.alarm = "OFFHOST_SPOOL_INDEX_INVALID_LOCAL_ARTIFACTS_RETAINED"

    def _persist_index(self):
        payload = {
            "version": VERSION,
            "records": [
                self.records[key].to_dict() for key in sorted(self.records)
            ],
        }
        temporary = self.index_path.with_name(
            self.index_path.name + f".{os.getpid()}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.index_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _pending(self):
        return [r for r in self.records.values() if r.state not in {"ACKNOWLEDGED", "PERMANENT_FAILURE", "CORRUPT"}]

    def health(self, *, enabled=False, backend_status="UNCONFIGURED_OFFHOST_BACKEND", restore_drill_last_status=None):
        pending = self._pending(); now = time.time()
        oldest = min((Path(r.artifact_path).stat().st_mtime for r in pending if Path(r.artifact_path).exists()), default=None)
        free = shutil.disk_usage(self.root).free
        return {
            "offhost_enabled": bool(enabled), "backend_status": backend_status,
            "pending_artifacts": len(pending), "pending_bytes": sum(r.byte_size for r in pending),
            "oldest_pending_age_seconds": (max(0.0, now-oldest) if oldest is not None else None),
            "last_success_at": self.last_success_at, "last_error": self.last_error,
            "checksum_failures": self.checksum_failures, "restore_drill_last_status": restore_drill_last_status,
            "storage_free_bytes": free, "storage_free_space_warning": free < self.warn_free_bytes, "alarm": self.alarm,
            "authority": False,
        }

    def enqueue(self, artifact_path, manifest_path, manifest):
        artifact_id = str(manifest["artifact_id"]); size = int(manifest["byte_size"])
        if artifact_id in self.records:
            return True
        pending = self._pending()
        if len(pending) >= self.max_items or sum(r.byte_size for r in pending) + size > self.max_bytes:
            self.alarm = "OFFHOST_SPOOL_QUEUE_FULL_LOCAL_ARTIFACT_RETAINED"
            return False
        self.records[artifact_id] = SpoolRecord(artifact_id, str(artifact_path), str(manifest_path), size)
        self._persist_index()
        return True

    def _retry_delay(self, attempts):
        return min(self.retry_cap, self.retry_base * (2 ** max(0, int(attempts)-1)))

    async def upload_once(self, backend, artifact_id, now=None):
        now = time.time() if now is None else float(now)
        record = self.records[artifact_id]
        if record.state == "ACKNOWLEDGED" or now < record.next_attempt_at:
            return record
        record = replace(record, state="UPLOADING", attempts=record.attempts+1); self.records[artifact_id] = record
        try:
            manifest = json.loads(Path(record.manifest_path).read_text(encoding="utf-8"))
            response = await asyncio.to_thread(backend.put_if_absent, record.artifact_path, manifest)
            status = str(response.get("status") or "RETRYABLE_FAILURE")
            if status == "ACKNOWLEDGED":
                head = await asyncio.to_thread(backend.head, artifact_id)
                if not head or head.get("sha256") != manifest.get("sha256"):
                    self.checksum_failures += 1; self.last_error = "OFFHOST_ACK_CHECKSUM_MISMATCH"
                    record = replace(record, state="CORRUPT", last_error=self.last_error)
                else:
                    self.last_success_at = now; self.last_error = None
                    record = replace(record, state="ACKNOWLEDGED", last_error=None)
            elif status == "CORRUPT":
                self.checksum_failures += 1; self.last_error = "OFFHOST_BACKEND_CORRUPT"
                record = replace(record, state="CORRUPT", last_error=self.last_error)
            elif status == "PERMANENT_FAILURE":
                self.last_error = str(response.get("error") or status)
                record = replace(record, state="PERMANENT_FAILURE", last_error=self.last_error)
            else:
                self.last_error = str(response.get("error") or status)
                record = replace(record, state="RETRYABLE_FAILURE", next_attempt_at=now+self._retry_delay(record.attempts), last_error=self.last_error)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}:{exc}"; self.alarm = "OFFHOST_UPLOAD_RETRYABLE_FAILURE_LOCAL_ARTIFACT_RETAINED"
            record = replace(record, state="RETRYABLE_FAILURE", next_attempt_at=now+self._retry_delay(record.attempts), last_error=self.last_error)
        self.records[artifact_id] = record
        self._persist_index()
        return record
