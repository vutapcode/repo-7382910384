"""Process-level singleton locks for SMC2026 service entrypoints."""

import fcntl
import os
from pathlib import Path


class DuplicateInstanceError(RuntimeError):
    """Raised when another healthy process already owns a service lock."""


class RuntimeLock:
    def __init__(self, name, runtime_dir=None):
        # Use a stable per-user state path by default. /tmp is unsafe with
        # systemd PrivateTmp because a manually started process may see a
        # different namespace and acquire a second "singleton" lock.
        default_dir = Path.home() / ".local" / "state" / "smc2026" / "runtime"
        base = Path(
            runtime_dir
            or os.getenv("SMC_RUNTIME_DIR")
            or os.getenv("XDG_RUNTIME_DIR")
            or default_dir
        )
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            base.chmod(0o700)
        except OSError:
            pass
        self.path = base / f"smc2026-{name}.lock"
        self.handle = self.path.open("a+", encoding="utf-8")

    def acquire(self):
        try:
            fcntl.flock(
                self.handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            self.handle.seek(0)
            owner = self.handle.read().strip() or "unknown"
            self.handle.close()
            raise DuplicateInstanceError(
                f"DUPLICATE_INSTANCE lock={self.path} owner_pid={owner}"
            ) from exc

        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        os.fsync(self.handle.fileno())
        return self

    def close(self):
        if self.handle.closed:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def acquire_runtime_lock(name, runtime_dir=None):
    canonical = "bot" if str(name) in {"bot", "bot_mainnet_shadow"} else str(name)
    return RuntimeLock(canonical, runtime_dir=runtime_dir).acquire()
