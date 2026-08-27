"""Whole-host CPU budget governor for the 2-vCPU Lightsail runtime.

The hot strategy never shells out through this module.  A five-second sampler
reads aggregate kernel counters and publishes a small RAM snapshot.  Rolling
budgets are normalized like Lightsail CPUUtilization: 100 percent means all
allocated vCPUs are busy.
"""

from collections import deque
import asyncio
import math
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


VERSION = "WSTRADE_HOST_CPU_GOVERNOR_V1"
DEFAULT_WINDOWS = (900, 3600)
BLOCKER_MARKERS = (
    "extensionhost", "vscode-server", "remote-cli", "codex",
    "gcc", "g++", "clang", "cc1", "rustc", "cargo", "pytest",
    "unittest", "recorder.replay", "apt ", "apt-get", "dpkg",
)


def _float_env(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _windows_env():
    raw = os.getenv("WSTRADE_CPU_WINDOWS_SECONDS", "900,3600")
    try:
        values = tuple(sorted({int(value.strip()) for value in raw.split(",")}))
    except (TypeError, ValueError):
        return DEFAULT_WINDOWS
    return values if values == DEFAULT_WINDOWS else DEFAULT_WINDOWS


def parse_proc_stat(text):
    line = next((row for row in str(text).splitlines() if row.startswith("cpu ")), "")
    fields = line.split()[1:]
    if len(fields) < 4:
        raise ValueError("PROC_STAT_CPU_MISSING")
    values = [int(value) for value in fields]
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle


def read_proc_stat(path="/proc/stat"):
    return parse_proc_stat(Path(path).read_text(encoding="ascii"))


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="cpu_health_", suffix=".tmp", dir=path.parent
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


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def _boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text(
            encoding='ascii'
        ).strip()
    except OSError:
        return ''


def process_snapshot():
    """Return top processes and production blockers with bounded output."""
    try:
        completed = subprocess.run(
            ("ps", "-eo", "pid=,pcpu=,comm=,args=", "--sort=-pcpu"),
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return [], []
    rows = []
    blockers = []
    own_pid = os.getpid()
    for line in completed.stdout.splitlines()[:40]:
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            raw_cpu = max(0.0, float(parts[1]))
        except ValueError:
            continue
        command = parts[2]
        args = parts[3] if len(parts) > 3 else command
        row = {
            "pid": pid,
            # ps reports 100 percent per fully used core. Normalize later UI
            # against the host's vCPU count.
            "cpu_core_pct": round(raw_cpu, 2),
            "command": command[:80],
        }
        rows.append(row)
        lowered = f"{command} {args}".lower()
        if pid != own_pid and raw_cpu >= 0.2 and any(
            marker in lowered for marker in BLOCKER_MARKERS
        ):
            blockers.append({**row, "reason": "INTERACTIVE_OR_BATCH_WORKLOAD"})
    return rows[:8], blockers[:8]


class HostCpuGovernor:
    def __init__(
        self, *, cpu_count=None, windows=None, target_pct=None, hard_pct=None,
        history_path=None,
    ):
        self.cpu_count = max(1, int(cpu_count or os.cpu_count() or 1))
        self.windows = tuple(windows or _windows_env())
        self.target_pct = float(
            _float_env("WSTRADE_CPU_TARGET_PCT", "15")
            if target_pct is None else target_pct
        )
        self.hard_pct = float(
            _float_env("WSTRADE_CPU_HARD_PCT", "20")
            if hard_pct is None else hard_pct
        )
        if not 0.0 < self.target_pct < self.hard_pct <= 100.0:
            raise ValueError("INVALID_CPU_BUDGET")
        self.samples = deque()
        self.previous = None
        self.mode = "WARMUP"
        self.top_processes = []
        self.production_blockers = []
        self.last_process_scan_mono = 0.0
        self.instant_pct = deque(maxlen=720)
        self.external_path = Path(os.getenv(
            "WSTRADE_LIGHTSAIL_CPU_PATH",
            "/home/ubuntu/smc2026_data/health/lightsail_cpu.json",
        ))
        self.health_path = Path(os.getenv(
            "WSTRADE_CPU_HEALTH_PATH",
            "/home/ubuntu/smc2026_data/health/cpu_status.json",
        ))
        self.history_path = Path(
            history_path or os.getenv(
                "WSTRADE_CPU_HISTORY_PATH",
                "/home/ubuntu/smc2026_data/health/cpu_history.json",
            )
        )
        self.process_started_mono = time.monotonic()
        self.process_started_wall = time.time()
        self.history_restored = False
        self._restore_history()

    def _restore_history(self):
        raw = _read_json(self.history_path)
        if not raw or raw.get('boot_id') != _boot_id():
            return False
        try:
            age = max(0.0, time.time() - float(raw.get('saved_at', 0.0)))
            # A long unsampled gap cannot be placed accurately inside both
            # rolling windows. Restart warmup is safer than inventing coverage.
            if age > 30.0:
                return False
            now_mono = time.monotonic()
            rows = []
            for item in list(raw.get('samples') or ())[-1000:]:
                stamp, busy, capacity, observed = map(float, item)
                if not all(math.isfinite(value) for value in (
                    stamp, busy, capacity, observed,
                )):
                    return False
                if (
                    stamp > now_mono + 1.0 or busy < 0.0 or capacity <= 0.0
                    or observed <= 0.0 or busy > capacity + 1e-6
                ):
                    return False
                if stamp >= now_mono - max(self.windows) - 30.0:
                    rows.append((stamp, busy, capacity, observed))
            previous = list(raw.get('previous') or ())
            if len(previous) != 3:
                return False
            previous_at = float(previous[0])
            previous_total = int(previous[1])
            previous_idle = int(previous[2])
            if previous_at > now_mono + 1.0 or previous_total < previous_idle:
                return False
        except (TypeError, ValueError, OverflowError):
            return False
        self.samples = deque(rows)
        self.previous = (previous_at, previous_total, previous_idle)
        self.history_restored = True
        return True

    def checkpoint(self):
        payload = {
            'schema_version': 1,
            'version': VERSION,
            'boot_id': _boot_id(),
            'saved_at': time.time(),
            'previous': list(self.previous) if self.previous is not None else None,
            'samples': [list(row) for row in self.samples],
        }
        _atomic_json(self.history_path, payload)
        return payload

    def _trim(self, now):
        cutoff = float(now) - max(self.windows)
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def _window(self, now, seconds):
        cutoff = float(now) - float(seconds)
        rows = [row for row in self.samples if row[0] >= cutoff]
        busy = sum(row[1] for row in rows)
        capacity = sum(row[2] for row in rows)
        observed = sum(row[3] for row in rows)
        pct = 100.0 * busy / capacity if capacity > 0.0 else 0.0
        full_budget = self.hard_pct / 100.0 * self.cpu_count * float(seconds)
        return {
            "pct": pct,
            "busy_cpu_seconds": busy,
            "capacity_cpu_seconds": capacity,
            "budget_remaining_cpu_seconds": max(0.0, full_budget - busy),
            "coverage_seconds": observed,
            "coverage_complete": observed >= float(seconds) * 0.98,
        }

    def _post_start_coverage(self, now, seconds):
        cutoff = max(float(now) - float(seconds), self.process_started_mono)
        coverage = 0.0
        for stamp, _, _, observed in self.samples:
            interval_end = min(float(stamp), float(now))
            interval_start = float(stamp) - float(observed)
            coverage += max(0.0, interval_end - max(interval_start, cutoff))
        return min(float(seconds), coverage)

    def _external(self, now_wall):
        payload = _read_json(self.external_path)
        updated_ms = int(payload.get("updated_at_ms", 0) or 0)
        age = None if updated_ms <= 0 else max(0.0, now_wall - updated_ms / 1000.0)
        values = []
        for key in ("cpu_15m_pct", "cpu_1h_pct"):
            try:
                values.append(float(payload.get(key)))
            except (TypeError, ValueError):
                pass
        def optional_int(key):
            try:
                return int(payload.get(key))
            except (TypeError, ValueError):
                return None
        return {
            "lightsail_cpu_last_seen": updated_ms or None,
            "metric_age_seconds": age,
            "metric_fresh": bool(age is not None and age <= 900.0 and values),
            "max_window_pct": max(values) if values else None,
            "lightsail_window_15m_start_ms": optional_int("window_15m_start_ms"),
            "lightsail_window_1h_start_ms": optional_int("window_1h_start_ms"),
        }

    def _choose_mode(self, peak):
        if peak >= 19.5:
            return "SAFETY_ONLY"
        if peak >= 18.5:
            return "DEFENSIVE"
        if peak >= 17.0:
            return "CONSERVE"
        return "NORMAL"

    def sample(self, *, now_mono=None, now_wall=None, counters=None, scan_processes=True):
        now_mono = time.monotonic() if now_mono is None else float(now_mono)
        now_wall = time.time() if now_wall is None else float(now_wall)
        total, idle = read_proc_stat() if counters is None else counters
        total, idle = int(total), int(idle)
        if self.previous is not None:
            previous_at, previous_total, previous_idle = self.previous
            elapsed = max(0.0, now_mono - previous_at)
            delta_total = total - previous_total
            delta_idle = idle - previous_idle
            if elapsed > 0.0 and delta_total > 0 and 0 <= delta_idle <= delta_total:
                ratio = max(0.0, min(1.0, (delta_total - delta_idle) / delta_total))
                capacity = elapsed * self.cpu_count
                self.samples.append((now_mono, ratio * capacity, capacity, elapsed))
                self.instant_pct.append(ratio * 100.0)
        self.previous = (now_mono, total, idle)
        self._trim(now_mono)

        if scan_processes and now_mono - self.last_process_scan_mono >= 15.0:
            self.top_processes, self.production_blockers = process_snapshot()
            self.last_process_scan_mono = now_mono

        windows = {seconds: self._window(now_mono, seconds) for seconds in self.windows}
        history_start_mono = (
            float(self.samples[0][0]) - float(self.samples[0][3])
            if self.samples else now_mono
        )
        history_start_wall_ms = int(
            (now_wall - max(0.0, now_mono - history_start_mono)) * 1000
        )
        actual_peak = max((item["pct"] for item in windows.values()), default=0.0)
        ordered = sorted(self.instant_pct)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))] if ordered else 0.0
        external = self._external(now_wall)
        instant_ratio = (self.instant_pct[-1] / 100.0) if self.instant_pct else 0.0
        projected = []
        for item in windows.values():
            horizon = min(60.0, max(5.0, item["coverage_seconds"] * 0.10))
            projected.append(100.0 * (
                item["busy_cpu_seconds"] + instant_ratio * self.cpu_count * horizon
            ) / max(
                item["capacity_cpu_seconds"] + self.cpu_count * horizon, 1e-12
            ))
        projected_peak = max(projected, default=actual_peak)
        external_peak = external["max_window_pct"] if external["metric_fresh"] else None
        control_peak = max(
            actual_peak, projected_peak,
            float(external_peak) if external_peak is not None else 0.0,
        )
        self.mode = self._choose_mode(control_peak)
        payload = {
            "schema_version": 1,
            "version": VERSION,
            "updated_at_ms": int(now_wall * 1000),
            "cpu_count": self.cpu_count,
            "target_pct": self.target_pct,
            "hard_pct": self.hard_pct,
            "host_cpu_15m_pct": round(windows[900]["pct"], 4),
            "host_cpu_1h_pct": round(windows[3600]["pct"], 4),
            "cpu_budget_15m_remaining": round(
                windows[900]["budget_remaining_cpu_seconds"], 4
            ),
            "cpu_budget_1h_remaining": round(
                windows[3600]["budget_remaining_cpu_seconds"], 4
            ),
            "coverage_15m_seconds": round(windows[900]["coverage_seconds"], 3),
            "coverage_1h_seconds": round(windows[3600]["coverage_seconds"], 3),
            "coverage_15m_complete": windows[900]["coverage_complete"],
            "coverage_1h_complete": windows[3600]["coverage_complete"],
            "local_window_15m_start_ms": int((now_wall - 900.0) * 1000),
            "local_window_1h_start_ms": int((now_wall - 3600.0) * 1000),
            "cpu_history_window_start_ms": history_start_wall_ms,
            "cpu_governor_started_at_ms": int(self.process_started_wall * 1000),
            "post_start_coverage_15m_seconds": round(
                self._post_start_coverage(now_mono, 900), 3
            ),
            "post_start_coverage_1h_seconds": round(
                self._post_start_coverage(now_mono, 3600), 3
            ),
            "governor_mode": self.mode,
            "host_cpu_p95_pct": round(p95, 4),
            "host_cpu_projected_peak_pct": round(projected_peak, 4),
            # Backward-compatible name: this is a production/live admission
            # gate.  Shadow execution must keep every otherwise eligible
            # sample; CPU pressure may only lower non-authoritative evaluation
            # cadence, never censor a demo trade.
            "entry_cpu_allowed": self.mode in ("NORMAL", "CONSERVE"),
            "live_entry_cpu_allowed": self.mode in ("NORMAL", "CONSERVE"),
            "shadow_entry_cpu_allowed": True,
            "hard_limit_respected": actual_peak < self.hard_pct and (
                external_peak is None or float(external_peak) < self.hard_pct
            ),
            "cpu_history_restored": self.history_restored,
            "top_cpu_processes": list(self.top_processes),
            "production_blockers": list(self.production_blockers),
            **external,
        }
        return payload

    def publish(self, state, payload):
        state.host_cpu_15m_pct = payload["host_cpu_15m_pct"]
        state.host_cpu_1h_pct = payload["host_cpu_1h_pct"]
        state.cpu_budget_15m_remaining = payload["cpu_budget_15m_remaining"]
        state.cpu_budget_1h_remaining = payload["cpu_budget_1h_remaining"]
        state.governor_mode = payload["governor_mode"]
        state.host_cpu_p95_pct = payload.get("host_cpu_p95_pct", 0.0)
        state.host_cpu_entry_allowed = payload["entry_cpu_allowed"]
        state.live_entry_cpu_allowed = payload.get(
            "live_entry_cpu_allowed", payload["entry_cpu_allowed"]
        )
        state.shadow_entry_cpu_allowed = bool(
            payload.get("shadow_entry_cpu_allowed", True)
        )
        state.host_cpu_hard_limit_respected = payload["hard_limit_respected"]
        state.host_cpu_top_processes = payload["top_cpu_processes"]
        state.production_workload_blockers = payload["production_blockers"]
        state.cpu_history_restored = payload.get("cpu_history_restored", False)
        state.cpu_history_window_start_ms = payload.get("cpu_history_window_start_ms")
        state.cpu_governor_started_at_ms = payload.get("cpu_governor_started_at_ms")
        state.cpu_post_start_coverage_15m_seconds = payload.get(
            "post_start_coverage_15m_seconds", 0.0
        )
        state.cpu_post_start_coverage_1h_seconds = payload.get(
            "post_start_coverage_1h_seconds", 0.0
        )
        state.lightsail_cpu_last_seen = payload["lightsail_cpu_last_seen"]
        state.lightsail_metric_age_seconds = payload["metric_age_seconds"]
        state.lightsail_metric_fresh = payload["metric_fresh"]
        state.host_cpu_snapshot = dict(payload)

    async def run(self, state, interval=5.0):
        last_write = 0.0
        while True:
            try:
                payload = self.sample()
                self.publish(state, payload)
                now = time.monotonic()
                if now - last_write >= 15.0:
                    await asyncio.to_thread(_atomic_json, self.health_path, payload)
                    await asyncio.to_thread(self.checkpoint)
                    last_write = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.host_cpu_entry_allowed = False
                state.governor_mode = "SAFETY_ONLY"
                state.host_cpu_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(max(1.0, float(interval)))


def entry_allowed(state):
    return bool(getattr(state, "host_cpu_entry_allowed", False))


def feature_delay(state, normal_delay):
    mode = str(getattr(state, "governor_mode", "WARMUP"))
    factor = {"NORMAL": 1.0, "CONSERVE": 2.0, "DEFENSIVE": 5.0,
              "SAFETY_ONLY": 10.0, "WARMUP": 1.0}.get(mode, 5.0)
    return max(float(normal_delay), float(normal_delay) * factor)
