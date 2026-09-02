"""Append-only WAL writer and closed-hour Parquet compactor."""

import asyncio
import logging
import os
import tempfile
import threading
import time
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq


PARQUET_SCHEMA = pa.schema((
    ('schema_version', pa.int16()),
    ('code_version', pa.string()),
    ('config_version', pa.string()),
    ('source', pa.string()),
    ('symbol', pa.string()),
    ('stream', pa.string()),
    ('event_contract_version', pa.string()),
    ('event_id', pa.string()),
    ('exchange_event_time_ms', pa.int64()),
    ('event_time_ms', pa.int64()),
    ('receive_time_ms', pa.int64()),
    ('receive_time_monotonic_ns', pa.int64()),
    ('available_time_ms', pa.int64()),
    ('available_time_monotonic_ns', pa.int64()),
    ('epoch', pa.int64()),
    ('source_health', pa.string()),
    ('payload_version', pa.string()),
    ('clock_offset_ms', pa.float64()),
    ('clock_jitter_ms', pa.float64()),
    ('clock_uncertainty_ms', pa.float64()),
    ('batching_uncertainty_ms', pa.float64()),
    ('temporal_uncertainty_ms', pa.float64()),
    ('temporal_status', pa.string()),
    ('sequence_start', pa.int64()),
    ('sequence_end', pa.int64()),
    ('previous_sequence', pa.int64()),
    ('payload_json', pa.binary()),
))

# This stream is already a compact/delta cache of the bot journal. Historical
# V1 rows can be hundreds of MiB each because they embedded full score history;
# converting those duplicates to Arrow causes >1 GiB RSS spikes. Keep its WAL
# under normal 24h retention and compact the actual causal `bot_event` stream.
COMPACTION_SKIPPED_STREAMS = frozenset({'bot_cycles_snapshot'})
CPU_HEALTH_PATH = Path(os.getenv(
    'WSTRADE_CPU_HEALTH_PATH', '/home/ubuntu/smc2026_data/health/cpu_status.json'
))


class CompactionCpuDeferred(RuntimeError):
    pass


def cpu_allows_compaction(path=None, now=None):
    """Parquet is optional; stale/unknown CPU state always preserves the WAL."""
    try:
        payload = json.loads(Path(path or CPU_HEALTH_PATH).read_text(encoding='utf-8'))
        updated_ms = int(payload.get('updated_at_ms', 0) or 0)
        now = time.time() if now is None else float(now)
        return bool(
            updated_ms > 0 and now - updated_ms / 1000.0 <= 30.0
            and float(payload.get('host_cpu_15m_pct', 100.0)) < 12.0
            and float(payload.get('host_cpu_1h_pct', 100.0)) < 12.0
            and float(payload.get('cpu_budget_15m_remaining', 0.0)) > 180.0
            and float(payload.get('cpu_budget_1h_remaining', 0.0)) > 720.0
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def utc_partition(receive_time_ms):
    moment = datetime.fromtimestamp(receive_time_ms / 1000.0, tz=timezone.utc)
    return moment.strftime('%Y-%m-%d'), moment.strftime('%H')


def wal_path(root, record):
    date, hour = utc_partition(record['receive_time_ms'])
    return root / 'raw' / 'wal' / record['stream'] / date / f'{hour}.jsonl'


def _write_batch(root, batch):
    grouped = defaultdict(list)
    counts = defaultdict(int)
    for record in batch:
        grouped[wal_path(root, record)].append(orjson.dumps(record) + b'\n')
        counts[record['stream']] += 1
    for path, rows in grouped.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'ab', buffering=0) as handle:
            handle.write(b''.join(rows))
            os.fsync(handle.fileno())
    return dict(counts)


def _parquet_row(record):
    return {
        'schema_version': int(record.get('schema_version', 1)),
        'code_version': str(record.get('code_version', '')),
        'config_version': str(record.get('config_version', '')),
        'source': str(record.get('source', '')),
        'symbol': str(record.get('symbol', '')),
        'stream': str(record.get('stream', '')),
        'event_contract_version': str(record.get('event_contract_version', '')),
        'event_id': str(record.get('event_id', '')),
        'exchange_event_time_ms': int(
            record.get('exchange_event_time_ms', record.get('event_time_ms', 0)) or 0
        ),
        'event_time_ms': int(record.get('event_time_ms', 0) or 0),
        'receive_time_ms': int(record.get('receive_time_ms', 0) or 0),
        'receive_time_monotonic_ns': int(
            record.get('receive_time_monotonic_ns', 0) or 0
        ),
        'available_time_ms': int(
            record.get('available_time_ms', record.get('receive_time_ms', 0)) or 0
        ),
        'available_time_monotonic_ns': int(
            record.get('available_time_monotonic_ns', 0) or 0
        ),
        'epoch': int(record.get('epoch', 0) or 0),
        'source_health': str(record.get('source_health', 'UNKNOWN')),
        'payload_version': str(record.get('payload_version', 'UNKNOWN')),
        'clock_offset_ms': float(record.get('clock_offset_ms', 0.0) or 0.0),
        'clock_jitter_ms': float(record.get('clock_jitter_ms', 0.0) or 0.0),
        'clock_uncertainty_ms': float(
            record.get('clock_uncertainty_ms', 0.0) or 0.0
        ),
        'batching_uncertainty_ms': float(
            record.get('batching_uncertainty_ms', 0.0) or 0.0
        ),
        'temporal_uncertainty_ms': float(
            record.get('temporal_uncertainty_ms', 0.0) or 0.0
        ),
        'temporal_status': str(record.get('temporal_status', 'UNSAFE_OR_UNKNOWN')),
        'sequence_start': record.get('sequence_start'),
        'sequence_end': record.get('sequence_end'),
        'previous_sequence': record.get('previous_sequence'),
        'payload_json': orjson.dumps(record.get('payload', {})),
    }


def _decode_wal_line(line):
    """Decode a WAL row and salvage a complete record after a torn prefix."""
    try:
        return orjson.loads(line), False
    except orjson.JSONDecodeError:
        marker = b'{"schema_version"'
        offset = line.find(marker, 1)
        while offset >= 0:
            try:
                return orjson.loads(line[offset:]), True
            except orjson.JSONDecodeError:
                offset = line.find(marker, offset + 1)
        return None, False


def compact_wal(
    wal_file, data_root, chunk_size=2_000, return_stats=False,
    chunk_bytes=16 * 1024 * 1024, cpu_guard=None,
):
    """Compact one WAL without letting large payloads exhaust recorder RAM.

    ``bot_cycles_snapshot`` records can be several MiB each, so a row-count
    limit alone is not a memory bound.  Flush on either rows or serialized
    payload bytes.  The byte limit is intentionally approximate: it bounds the
    dominant binary column while leaving enough headroom for Arrow conversion.
    """
    wal_file = Path(wal_file)
    relative = wal_file.relative_to(data_root / 'raw' / 'wal')
    stream, date, filename = relative.parts
    hour = Path(filename).stem
    target = data_root / 'raw' / 'parquet' / stream / date / f'{hour}.parquet'
    if target.exists():
        return (None, 0, 0) if return_stats else None
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f'{hour}_', suffix='.parquet.tmp', dir=target.parent)
    os.close(fd)
    writer = None
    rows = []
    rows_bytes = 0
    recovered_rows = 0
    dropped_rows = 0

    def flush_rows():
        nonlocal writer, rows_bytes
        if not rows:
            return
        # Shutdown/CPU revocation must be observed before Arrow conversion or
        # zstd writing. Checking only after a large flush can exceed systemd's
        # stop timeout and leave the WAL process to be SIGKILLed.
        if cpu_guard is not None and not cpu_guard():
            raise CompactionCpuDeferred('CPU_BUDGET_CHANGED_BEFORE_COMPACTION')
        table = pa.Table.from_pylist(rows, schema=PARQUET_SCHEMA)
        if writer is None:
            writer = pq.ParquetWriter(temp_name, PARQUET_SCHEMA, compression='zstd')
        writer.write_table(table)
        rows.clear()
        rows_bytes = 0
        del table
        if cpu_guard is not None and not cpu_guard():
            raise CompactionCpuDeferred('CPU_BUDGET_CHANGED_DURING_COMPACTION')

    try:
        with open(wal_file, 'rb') as handle:
            for line in handle:
                if cpu_guard is not None and not cpu_guard():
                    raise CompactionCpuDeferred(
                        'CPU_BUDGET_CHANGED_DURING_WAL_SCAN'
                    )
                if not line.strip():
                    continue
                record, recovered = _decode_wal_line(line)
                if record is None:
                    dropped_rows += 1
                    continue
                recovered_rows += int(recovered)
                row = _parquet_row(record)
                rows.append(row)
                rows_bytes += len(row['payload_json']) + 512
                if len(rows) >= chunk_size or rows_bytes >= chunk_bytes:
                    flush_rows()
        flush_rows()
        if writer is None:
            result = (None, recovered_rows, dropped_rows)
            return result if return_stats else None
        writer.close()
        writer = None
        os.replace(temp_name, target)
        result = (target, recovered_rows, dropped_rows)
        return result if return_stats else target
    finally:
        if writer is not None:
            writer.close()
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        # Arrow may retain the just-compacted buffers in its process-wide
        # allocator. Return unused pages so a large historical partition does
        # not permanently raise the recorder daemon's RSS.
        try:
            pa.default_memory_pool().release_unused()
        except (AttributeError, NotImplementedError):
            pass


def closed_wals(data_root, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    current = now.strftime('%Y-%m-%d/%H.jsonl')
    base = data_root / 'raw' / 'wal'
    if not base.exists():
        return []
    result = []
    for path in base.glob('*/*/*.jsonl'):
        relative = path.relative_to(base)
        marker = f'{relative.parts[1]}/{relative.parts[2]}'
        if marker < current:
            result.append(path)
    return sorted(result)


def _partition_hour(path, base, suffix):
    """Parse only the recorder-owned stream/YYYY-MM-DD/HH partitions."""
    try:
        relative = path.relative_to(base)
    except ValueError:
        return None
    if len(relative.parts) != 3 or path.suffix != suffix:
        return None
    _, date, filename = relative.parts
    try:
        return datetime.strptime(
            f'{date}/{Path(filename).stem}', '%Y-%m-%d/%H'
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def prune_expired_partitions(data_root, retention_hours=24, now=None):
    """Delete recorder raw partitions outside a strict UTC retention window.

    A whole boundary hour is removed when its start precedes the exact cutoff.
    This deliberately retains at most ``retention_hours`` (often 23-24 hours)
    instead of keeping a partial partition that contains records older than the
    configured limit. Derived research data, health, metadata and bot ROM are
    outside the two allowlisted roots and can never be touched here.
    """
    root = Path(data_root).resolve()
    if root in (Path('/'), Path('/home'), Path('/home/ubuntu')):
        raise ValueError(f'Unsafe recorder data root: {root}')
    hours = int(retention_hours)
    if hours <= 0:
        raise ValueError('retention_hours must be positive')
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    deleted_files = 0
    deleted_bytes = 0
    for base, suffix in (
        (root / 'raw' / 'wal', '.jsonl'),
        (root / 'raw' / 'parquet', '.parquet'),
    ):
        if not base.exists():
            continue
        for path in base.glob('*/*/*'):
            partition = _partition_hour(path, base, suffix)
            if partition is None or partition >= cutoff:
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                continue
            deleted_files += 1
            deleted_bytes += size
        # Remove only empty descendants of the allowlisted recorder roots.
        directories = sorted(
            (item for item in base.glob('*/*') if item.is_dir()),
            key=lambda item: len(item.parts), reverse=True,
        )
        directories.extend(item for item in base.glob('*') if item.is_dir())
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass
    return {
        'cutoff_utc': cutoff.isoformat(),
        'files_deleted': deleted_files,
        'bytes_deleted': deleted_bytes,
    }


class AppendOnlyStore:
    def __init__(self, config, health):
        self.config = config
        self.health = health
        self.queue = asyncio.Queue(maxsize=config.queue_max)
        self._stop_token = object()
        self._maintenance_lock = asyncio.Lock()
        self._shutdown_requested = threading.Event()

    def request_shutdown(self):
        """Make in-flight compaction yield before asyncio joins its worker."""
        self._shutdown_requested.set()

    def _compaction_allowed(self):
        return not self._shutdown_requested.is_set() and cpu_allows_compaction()

    def publish(self, record):
        try:
            self.queue.put_nowait(record)
            size = self.queue.qsize()
            self.health.queue_size = size
            self.health.queue_max_seen = max(self.health.queue_max_seen, size)
            return True
        except asyncio.QueueFull:
            self.health.dropped += 1
            self.health.error('queue', 'QUEUE_FULL_EVENT_DROPPED')
            return False

    async def writer_loop(self):
        while True:
            batch = []
            stop_after_batch = False
            try:
                first = await self.queue.get()
                if first is self._stop_token:
                    self.queue.task_done()
                    self.health.queue_size = self.queue.qsize()
                    return
                batch.append(first)
                deadline = time.monotonic() + self.config.flush_interval
                while len(batch) < self.config.batch_max:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(self.queue.get(), remaining)
                        if item is self._stop_token:
                            # The sentinel can arrive while this writer is still
                            # coalescing a batch. Do not serialize it as a row.
                            self.queue.task_done()
                            stop_after_batch = True
                            break
                        batch.append(item)
                    except asyncio.TimeoutError:
                        break
                counts = await asyncio.to_thread(_write_batch, self.config.data_root, batch)
                for stream, count in counts.items():
                    self.health.written[stream] += count
            except asyncio.CancelledError:
                if batch:
                    try:
                        counts = await asyncio.to_thread(
                            _write_batch, self.config.data_root, batch
                        )
                        for stream, count in counts.items():
                            self.health.written[stream] += count
                    except Exception as exc:
                        self.health.writer_errors += 1
                        self.health.error('writer_shutdown', exc)
                raise
            except Exception as exc:
                self.health.writer_errors += 1
                self.health.error('writer', exc)
                await asyncio.sleep(1)
            finally:
                for _ in batch:
                    self.queue.task_done()
                self.health.queue_size = self.queue.qsize()
            if stop_after_batch:
                return

    async def stop_writer(self):
        """Drain then stop the writer without a cancellation/to_thread race."""
        await self.queue.put(self._stop_token)
        await self.queue.join()

    async def compactor_loop(self):
        while True:
            try:
                if not cpu_allows_compaction():
                    self.health.sampled_out['parquet_compaction_cpu_deferred'] += 1
                    await asyncio.sleep(60)
                    continue
                for path in closed_wals(self.config.data_root):
                    try:
                        stream = path.relative_to(
                            self.config.data_root / 'raw' / 'wal'
                        ).parts[0]
                    except (ValueError, IndexError):
                        stream = ''
                    if stream in COMPACTION_SKIPPED_STREAMS:
                        self.health.sampled_out[
                            f'parquet_compaction_skipped.{stream}'
                        ] += 1
                        continue
                    async with self._maintenance_lock:
                        # Retention may have removed this partition while the
                        # compactor was yielding between files.
                        if not path.exists():
                            continue
                        try:
                            target, recovered, dropped = await asyncio.to_thread(
                                compact_wal, path, self.config.data_root,
                                2_000, True, 16 * 1024 * 1024,
                                self._compaction_allowed,
                            )
                        except CompactionCpuDeferred:
                            self.health.sampled_out[
                                'parquet_compaction_cpu_interrupted'
                            ] += 1
                            break
                    self.health.sampled_out['wal_corrupt_row_recovered'] += recovered
                    self.health.sampled_out['wal_corrupt_row_dropped'] += dropped
                    if recovered or dropped:
                        logging.warning(
                            '[RECORDER] WAL damage %s recovered=%s dropped=%s',
                            path, recovered, dropped,
                        )
                    if target is not None:
                        self.health.parquet_files += 1
                        # Parquet was atomically installed; the duplicate WAL
                        # can now be retired without risking data loss.
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                self.health.connection('compactor', True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.error('compactor', exc)
            await asyncio.sleep(60)

    async def prune_once(self):
        async with self._maintenance_lock:
            result = await asyncio.to_thread(
                prune_expired_partitions,
                self.config.data_root,
                self.config.retention_hours,
            )
        self.health.retention_files_deleted += result['files_deleted']
        self.health.retention_bytes_deleted += result['bytes_deleted']
        self.health.retention_last_run_ms = time.time_ns() // 1_000_000
        return result

    async def retention_loop(self):
        while True:
            try:
                await self.prune_once()
                self.health.connection('retention', True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.error('retention', exc)
            await asyncio.sleep(self.config.retention_interval)
