"""Phase-7 non-blocking integration seam. Not wired into recorder storage."""
from __future__ import annotations
import os

VERSION = "OFFHOST_INTEGRATION_SEAM_V1"
AUTHORITY = False
FLAG = "WSTRADE_OFFHOST_DURABILITY_ENABLED"


def enabled(env=None):
    env = os.environ if env is None else env
    return str(env.get(FLAG, "false")).strip().lower() in {"1","true","yes","on"}


def enqueue_closed_reference(spool, artifact_path, manifest_path, manifest, *, env=None):
    """O(1) reference enqueue only; caller retains local artifact regardless."""
    if not enabled(env):
        return {"enqueued": False, "reason": "OFFHOST_DISABLED", "authority": False}
    accepted = spool.enqueue(artifact_path, manifest_path, manifest)
    return {
        "enqueued": bool(accepted),
        "reason": "ENQUEUED_REFERENCE" if accepted else "QUEUE_FULL_LOCAL_ARTIFACT_RETAINED",
        "authority": False,
    }
