#!/usr/bin/env python3
"""Restore/verify an immutable off-host WAL artifact into private staging only."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile

from recorder.offhost_durability import VERSION as MANIFEST_VERSION, DurabilityError
from recorder.offhost_backends.filesystem import FilesystemBackend

FINAL_STATES = frozenset({
    "RESTORE_VERIFIED", "CHECKSUM_MISMATCH", "MANIFEST_INVALID",
    "REPLAY_NONDETERMINISTIC", "VERSION_BOUNDARY_MISMATCH", "ARTIFACT_MISSING",
})


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_destination(staging, relative):
    relative = Path(str(relative or ""))
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise DurabilityError("PATH_TRAVERSAL_REJECTED")
    destination = Path(staging) / "restored" / relative
    root = (Path(staging) / "restored").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in [destination.parent, *destination.parents] if parent.exists()):
        raise DurabilityError("SYMLINK_ESCAPE_REJECTED")
    if root not in destination.resolve(strict=False).parents:
        raise DurabilityError("PATH_ESCAPE_REJECTED")
    return destination


def _version_match(manifest, expected):
    for field, value in (expected or {}).items():
        if value is not None and str(manifest.get(field)) != str(value):
            return False, field
    return True, None


def restore_and_replay(backend, artifact_id, *, replay_runner=None, expected_versions=None, staging_parent=None):
    try:
        manifest = backend.head(artifact_id)
    except Exception:
        manifest = None
    if not manifest:
        return {"status": "ARTIFACT_MISSING", "artifact_id": artifact_id}
    if manifest.get("durability_schema_version") != MANIFEST_VERSION or manifest.get("artifact_id") != artifact_id:
        return {"status": "MANIFEST_INVALID", "artifact_id": artifact_id}
    if not manifest.get("source_relative_path") or not manifest.get("sha256"):
        return {"status": "MANIFEST_INVALID", "artifact_id": artifact_id}
    ok, field = _version_match(manifest, expected_versions)
    if not ok:
        return {"status": "VERSION_BOUNDARY_MISMATCH", "artifact_id": artifact_id, "field": field}
    if not manifest.get("canonical_replay_hash"):
        return {"status": "VERSION_BOUNDARY_MISMATCH", "artifact_id": artifact_id, "reason": "SEALED_CANONICAL_REPLAY_HASH_MISSING"}
    if replay_runner is None:
        return {"status": "VERSION_BOUNDARY_MISMATCH", "artifact_id": artifact_id, "reason": "CANONICAL_REPLAY_ADAPTER_UNCONFIGURED"}

    staging = Path(tempfile.mkdtemp(prefix="wstrade-restore-", dir=staging_parent))
    os.chmod(staging, 0o700)
    try:
        destination = _safe_destination(staging, manifest["source_relative_path"])
        try:
            restored_manifest = backend.get(artifact_id, destination)
        except FileNotFoundError:
            return {"status": "ARTIFACT_MISSING", "artifact_id": artifact_id}
        if restored_manifest != manifest:
            return {"status": "MANIFEST_INVALID", "artifact_id": artifact_id}
        if destination.stat().st_size != int(manifest.get("byte_size", -1)) or _sha256(destination) != manifest["sha256"]:
            return {"status": "CHECKSUM_MISMATCH", "artifact_id": artifact_id}
        first = str(replay_runner(staging / "restored"))
        second = str(replay_runner(staging / "restored"))
        if first != second:
            return {"status": "REPLAY_NONDETERMINISTIC", "artifact_id": artifact_id, "first_hash": first, "second_hash": second}
        if first != str(manifest["canonical_replay_hash"]):
            return {"status": "CHECKSUM_MISMATCH", "artifact_id": artifact_id, "reason": "SEALED_REPLAY_HASH_MISMATCH"}
        return {
            "status": "RESTORE_VERIFIED", "artifact_id": artifact_id,
            "content_sha256": manifest["sha256"], "replay_hash": first,
            "production_copy_performed": False,
        }
    except DurabilityError:
        return {"status": "MANIFEST_INVALID", "artifact_id": artifact_id}


def _load_runner(spec):
    if not spec:
        return None
    module_name, sep, function_name = spec.partition(":")
    if not sep:
        raise ValueError("replay adapter must be module:function")
    return getattr(importlib.import_module(module_name), function_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_id")
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--replay-adapter")
    parser.add_argument("--expected-code-version")
    parser.add_argument("--expected-config-version")
    args = parser.parse_args()
    report = restore_and_replay(
        FilesystemBackend(args.backend_root), args.artifact_id,
        replay_runner=_load_runner(args.replay_adapter),
        expected_versions={
            "code_version": args.expected_code_version,
            "config_version": args.expected_config_version,
        },
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "RESTORE_VERIFIED" else 2

if __name__ == "__main__":
    raise SystemExit(main())
