"""Recorder health counters and atomic status snapshots."""

import asyncio
import os
import shutil
import tempfile
import time
from collections import Counter, deque

import orjson


class HealthState:
    def __init__(self, config):
        self.config = config
        self.started_at_ms = time.time_ns() // 1_000_000
        self.received = Counter()
        self.written = Counter()
        self.sampled_out = Counter()
        self.last_event_ms = {}
        self.connections = {}
        self.reconnects = Counter()
        self.errors = Counter()
        self.dropped = 0
        self.queue_size = 0
        self.queue_max_seen = 0
        self.writer_errors = 0
        self.depth_synced = False
        self.depth_last_u = None
        self.depth_gaps = 0
        self.sequence_gaps = Counter()
        self.depth_checkpoints = 0
        self.last_error = None
        self.component_health = {}
        self.current_status = 'OK'
        self.recovered_at_ms = None
        self.consecutive_errors = 0
        self.late_event_count_by_source = Counter()
        self.late_event_delays_ms = deque(maxlen=20000)
        self.parquet_files = 0
        self.retention_files_deleted = 0
        self.retention_bytes_deleted = 0
        self.retention_last_run_ms = None
        self.code_version = None
        self.config_version = None
        self.wavefront_shadow = None

    def saw(self, stream, event_time_ms):
        self.received[stream] += 1
        self.last_event_ms[stream] = int(event_time_ms or 0)

    def connection(self, name, connected):
        self.connections[name] = bool(connected)
        now_ms = time.time_ns() // 1_000_000
        component = self.component_health.setdefault(name, {
            'current_status': 'OK', 'consecutive_errors': 0,
            'recovered_at_ms': None,
        })
        if connected:
            if component['current_status'] != 'OK':
                component['recovered_at_ms'] = now_ms
                self.recovered_at_ms = now_ms
            component['current_status'] = 'OK'
            component['consecutive_errors'] = 0
        else:
            component['current_status'] = 'DEGRADED'
        self.consecutive_errors = sum(
            int(item.get('consecutive_errors', 0))
            for item in self.component_health.values()
        )
        component_states = {
            item.get('current_status') for item in self.component_health.values()
        }
        self.current_status = (
            'ERROR' if 'ERROR' in component_states
            else 'DEGRADED' if (
                'DEGRADED' in component_states
                or any(not value for value in self.connections.values())
            ) else 'OK'
        )

    def error(self, name, error):
        self.errors[name] += 1
        self.last_error = {
            'component': name,
            'message': str(error),
            'at_ms': time.time_ns() // 1_000_000,
        }
        component = self.component_health.setdefault(name, {
            'current_status': 'OK', 'consecutive_errors': 0,
            'recovered_at_ms': None,
        })
        component['current_status'] = 'ERROR'
        component['consecutive_errors'] += 1
        self.consecutive_errors = sum(
            int(item.get('consecutive_errors', 0))
            for item in self.component_health.values()
        )
        self.current_status = 'ERROR'

    def late_event(self, source, delay_ms):
        self.late_event_count_by_source[str(source or 'unknown')] += 1
        self.late_event_delays_ms.append(max(0, int(delay_ms or 0)))

    def _late_delay_summary(self):
        values = sorted(self.late_event_delays_ms)
        if not values:
            return {'p50': 0, 'p95': 0, 'max': 0, 'sample_size': 0}
        def percentile(fraction):
            index = min(len(values) - 1, int((len(values) - 1) * fraction + 0.5))
            return values[index]
        return {
            'p50': percentile(0.50), 'p95': percentile(0.95),
            'max': values[-1], 'sample_size': len(values),
        }

    def snapshot(self):
        now_ms = time.time_ns() // 1_000_000
        usage = shutil.disk_usage(self.config.data_root)
        disk_free_ratio = usage.free / usage.total if usage.total else 0.0
        disk_pressure = bool(
            usage.free < 5 * 1024 * 1024 * 1024 or disk_free_ratio < 0.10
        )
        operational_problem = bool(
            self.dropped or self.writer_errors or self.depth_gaps
            or sum(self.sequence_gaps.values())
            or any(not value for value in self.connections.values())
            or disk_pressure
        )
        current_status = self.current_status
        if current_status != 'ERROR':
            current_status = 'DEGRADED' if operational_problem else 'OK'
        return {
            'schema_version': 1,
            'status': (
                'DEGRADED'
                if current_status != 'OK'
                else 'RUNNING'
            ),
            'current_status': current_status,
            'recovered_at_ms': self.recovered_at_ms,
            'consecutive_errors': self.consecutive_errors,
            'component_health': {
                name: dict(value) for name, value in self.component_health.items()
            },
            'symbol': self.config.symbol,
            'code_version': self.code_version,
            'config_version': self.config_version,
            'wavefront_shadow': self.wavefront_shadow,
            'started_at_ms': self.started_at_ms,
            'updated_at_ms': now_ms,
            'uptime_seconds': max(0.0, (now_ms - self.started_at_ms) / 1000.0),
            'connections': dict(self.connections),
            'reconnects': dict(self.reconnects),
            'received': dict(self.received),
            'written': dict(self.written),
            'sampled_out': dict(self.sampled_out),
            'last_event_ms': dict(self.last_event_ms),
            'event_age_ms': {
                name: max(0, now_ms - timestamp)
                for name, timestamp in self.last_event_ms.items()
                if timestamp > 0
            },
            'queue': {
                'size': self.queue_size,
                'max_seen': self.queue_max_seen,
                'capacity': self.config.queue_max,
                'dropped': self.dropped,
            },
            'depth': {
                'synced': self.depth_synced,
                'last_u': self.depth_last_u,
                'gaps': self.depth_gaps,
                'checkpoints': self.depth_checkpoints,
            },
            'sequence_gaps': dict(self.sequence_gaps),
            'sequence_gap_total': sum(self.sequence_gaps.values()),
            'writer_errors': self.writer_errors,
            'parquet_files': self.parquet_files,
            'retention': {
                'hours': self.config.retention_hours,
                'files_deleted': self.retention_files_deleted,
                'bytes_deleted': self.retention_bytes_deleted,
                'last_run_ms': self.retention_last_run_ms,
            },
            'last_error': self.last_error,
            'last_error_at_ms': (
                self.last_error.get('at_ms') if self.last_error else None
            ),
            'late_events': {
                'count_total': sum(self.late_event_count_by_source.values()),
                'count_by_source': dict(self.late_event_count_by_source),
                'delay_ms': self._late_delay_summary(),
            },
            'disk': {
                'total_bytes': usage.total,
                'used_bytes': usage.used,
                'free_bytes': usage.free,
                'free_ratio': disk_free_ratio,
                'pressure': disk_pressure,
            },
        }


def _atomic_status(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='health_', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


async def health_loop(health):
    path = health.config.data_root / 'health' / 'status.json'
    while True:
        try:
            await asyncio.to_thread(_atomic_status, path, health.snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            health.writer_errors += 1
            health.error('health_writer', exc)
        await asyncio.sleep(health.config.health_interval)
