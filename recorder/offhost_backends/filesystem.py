"""Filesystem test backend for the Phase-7 durability contract.

This is NOT an off-host production backend. It exists only to verify atomic,
idempotent artifact semantics without adding a cloud dependency.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from recorder.offhost_durability import DurabilityError

STATUS = "FILESYSTEM_TEST_BACKEND_NOT_OFFHOST"


class FilesystemBackend:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def _dir(self, artifact_id):
        if not artifact_id or any(ch not in "0123456789abcdef" for ch in artifact_id.lower()):
            raise DurabilityError("ARTIFACT_ID_INVALID")
        return self.artifacts / artifact_id.lower()

    def put_if_absent(self, artifact, manifest):
        artifact = Path(artifact)
        artifact_id = str(manifest.get("artifact_id") or "")
        final = self._dir(artifact_id)
        if final.exists():
            current = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
            if current.get("sha256") == manifest.get("sha256"):
                return {"status": "ACKNOWLEDGED", "artifact_id": artifact_id, "created": False}
            return {"status": "CORRUPT", "artifact_id": artifact_id, "created": False}
        temp = Path(tempfile.mkdtemp(prefix=f".{artifact_id}.", dir=self.artifacts))
        try:
            shutil.copyfile(artifact, temp / "artifact.bin")
            with (temp / "manifest.json").open("w", encoding="utf-8") as handle:
                json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush(); os.fsync(handle.fileno())
            os.chmod(temp / "artifact.bin", 0o600)
            os.chmod(temp / "manifest.json", 0o600)
            os.replace(temp, final)
            return {"status": "ACKNOWLEDGED", "artifact_id": artifact_id, "created": True}
        finally:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)

    def head(self, artifact_id):
        final = self._dir(artifact_id)
        manifest = final / "manifest.json"
        if not manifest.exists():
            return None
        return json.loads(manifest.read_text(encoding="utf-8"))

    def get(self, artifact_id, destination):
        final = self._dir(artifact_id)
        source = final / "artifact.bin"
        manifest = final / "manifest.json"
        if not source.exists() or not manifest.exists():
            raise FileNotFoundError(artifact_id)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
        os.close(fd)
        try:
            shutil.copyfile(source, temp_name)
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, destination)
        finally:
            try: os.unlink(temp_name)
            except FileNotFoundError: pass
        return json.loads(manifest.read_text(encoding="utf-8"))
