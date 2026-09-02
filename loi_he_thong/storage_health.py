"""Single physical storage-health owner for recorder and trading runtime."""

from __future__ import annotations

import os
from pathlib import Path


VERSION = "STORAGE_HEALTH_V1_SHARED_OWNER"
MIN_FREE_BYTES = int(os.environ.get(
    "WSTRADE_STORAGE_MIN_FREE_BYTES",
    os.environ.get("SMC_MIN_FREE_DISK_BYTES", str(5 * 1024 * 1024 * 1024)),
))
MIN_FREE_RATIO = float(os.environ.get(
    "WSTRADE_STORAGE_MIN_FREE_RATIO",
    os.environ.get("SMC_MIN_FREE_DISK_RATIO", "0.10"),
))


def measure(path):
    target = Path(path).expanduser()
    while not target.exists() and target != target.parent:
        target = target.parent
    stats = os.statvfs(str(target))
    block = int(stats.f_frsize or stats.f_bsize or 1)
    free_bytes = int(stats.f_bavail) * block
    total_bytes = int(stats.f_blocks) * block
    used_bytes = max(0, total_bytes - free_bytes)
    free_ratio = 0.0 if total_bytes <= 0 else free_bytes / total_bytes
    pressure = bool(
        free_bytes < MIN_FREE_BYTES or free_ratio < MIN_FREE_RATIO
    )
    return {
        "version": VERSION,
        "path": str(target),
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "free_ratio": free_ratio,
        "minimum_free_bytes": MIN_FREE_BYTES,
        "minimum_free_ratio": MIN_FREE_RATIO,
        "pressure": pressure,
        "status": "UNSAFE" if pressure else "HEALTHY",
    }
