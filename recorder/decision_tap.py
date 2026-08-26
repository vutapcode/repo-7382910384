"""Read-only tail of the bot's durable journal and cycle snapshot."""

import asyncio
import hashlib
import os
import tempfile
import time

import orjson


_CYCLE_FIELDS = (
    'position_cycle_id', 'setup_id', 'setup_generation', 'symbol', 'mode',
    'setup_type', 'setup_zone_id', 'setup_kind', 'opportunity_id', 'bias',
    'created_at', 'status', 'entry_style', 'execution_purpose',
    'economic_result_valid', 'economic_invalid_reason', 'signal_venue',
    'execution_venue', 'signal_price', 'decision_price',
    'entry_reference_price', 'requested_qty', 'score_version', 'score_100',
    'abort_reason', 'exit_reason', 'closed_at', 'holding_time_ms',
    'path_calibration_label',
)

_ACTUAL_FIELDS = (
    'valid_for_strategy_evaluation', 'entry_fill_ids', 'exit_fill_ids',
    'entry_fill_price', 'exit_fill_price', 'gross_pnl_quote', 'fee_quote',
    'net_pnl_quote', 'gross_pnl_bps', 'fee_bps', 'net_pnl_bps', 'opened_at',
    'cross_venue_entry_gap_bps', 'integrity',
)


def _pick(source, fields):
    source = source if isinstance(source, dict) else {}
    return {field: source.get(field) for field in fields if field in source}


def _compact_cycle(cycle):
    compact = _pick(cycle, _CYCLE_FIELDS)
    allocation = cycle.get('allocation_policy') or {}
    compact['allocation'] = _pick(allocation, (
        'size_pct', 'target_notional_pct', 'allocation_unit', 'tier',
        'trade_power_100', 'activation_floor_100', 'policy_version',
    ))
    economic = cycle.get('economic_observation') or {}
    compact['economics'] = _pick(economic, (
        'policy_mode', 'blocks_entry', 'expected_net_edge_bps',
        'realizable_edge_lcb', 'checkpoint_monetizable', 'reason',
    ))
    plan = cycle.get('dynamic_exit_plan') or {}
    compact['dynamic_exit'] = _pick(plan, (
        'policy', 'decision', 'realizable_edge_lcb',
        'checkpoint_monetizable', 'checkpoint_lock_net_bps',
        'trailing_applicable', 'tp1_price', 'tp1_allocation', 'tp2_price',
    ))
    compact['actual'] = _pick(cycle.get('actual'), _ACTUAL_FIELDS)
    return compact


def compact_cycles_delta(payload, previous_fingerprints=None):
    """Return bounded cycle deltas instead of duplicating the full journal.

    The append-only bot event stream remains the detailed source of truth. This
    snapshot provides a causal cycle index and final economics without copying
    nested orders, score histories, shadow registries, and counterfactuals every
    ten seconds.
    """
    previous_fingerprints = previous_fingerprints or {}
    current_fingerprints = {}
    changed = []
    status_counts = {}
    for cycle in payload.get('cycles', ()) if isinstance(payload, dict) else ():
        if not isinstance(cycle, dict):
            continue
        cycle_id = str(cycle.get('position_cycle_id') or '')
        if not cycle_id:
            continue
        compact = _compact_cycle(cycle)
        encoded = orjson.dumps(compact, option=orjson.OPT_SORT_KEYS)
        fingerprint = hashlib.sha256(encoded).hexdigest()[:16]
        current_fingerprints[cycle_id] = fingerprint
        status = str(cycle.get('status') or 'UNKNOWN')
        status_counts[status] = status_counts.get(status, 0) + 1
        if previous_fingerprints.get(cycle_id) != fingerprint:
            changed.append(compact)
    removed = sorted(set(previous_fingerprints) - set(current_fingerprints))
    snapshot = {
        'snapshot_version': 'CYCLE_DELTA_V2',
        'schema_version': payload.get('schema_version') if isinstance(payload, dict) else None,
        'journal_updated_at': payload.get('updated_at') if isinstance(payload, dict) else None,
        'code_version': payload.get('code_version') if isinstance(payload, dict) else None,
        'strategy_config_version': (
            payload.get('strategy_config_version') if isinstance(payload, dict) else None
        ),
        'cycle_count': len(current_fingerprints),
        'status_counts': status_counts,
        'changed_cycles': changed,
        'removed_cycle_ids': removed,
    }
    return snapshot, current_fingerprints


def _atomic_offset(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='offset_', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(orjson.dumps(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _load_offset(path):
    try:
        payload = orjson.loads(path.read_bytes())
        return (
            int(payload.get('offset', 0)),
            int(payload.get('device', 0) or 0),
            int(payload.get('inode', 0) or 0),
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0, 0


class DecisionTap:
    def __init__(self, config, emit, health=None):
        self.config = config
        self.emit = emit
        self.health = health
        self.offset_path = config.data_root / 'metadata' / 'decision_tail_offset.json'
        self.offset, self.event_device, self.event_inode = _load_offset(
            self.offset_path
        )
        self.last_cycles_mtime_ns = 0
        self.last_cycles_emit_mono = 0.0
        self.cycle_fingerprints = {}

    def _read_new_events(self):
        path = self.config.journal_events_path
        if not path.exists():
            return [], self.offset
        stat = path.stat()
        identity = (int(stat.st_dev), int(stat.st_ino))
        if identity != (self.event_device, self.event_inode):
            self.offset = 0
            self.event_device, self.event_inode = identity
        elif stat.st_size < self.offset:
            self.offset = 0
        rows = []
        with open(path, 'rb') as handle:
            handle.seek(self.offset)
            while True:
                line_start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b'\n'):
                    handle.seek(line_start)
                    break
                try:
                    rows.append(orjson.loads(line))
                except orjson.JSONDecodeError:
                    if self.health is not None:
                        self.health.decision_tap_parse_errors += 1
                        self.health.errors['decision_tap_parse_errors'] += 1
                    continue
            new_offset = handle.tell()
        return rows, new_offset

    async def loop(self):
        while True:
            try:
                rows, new_offset = await asyncio.to_thread(self._read_new_events)
                for event in rows:
                    event_time_ms = int(float(event.get('ts', time.time())) * 1000)
                    self.emit('bot_event', event, event_time_ms=event_time_ms)
                if new_offset != self.offset:
                    self.offset = new_offset
                    await asyncio.to_thread(
                        _atomic_offset, self.offset_path, {
                            'offset': self.offset,
                            'device': self.event_device,
                            'inode': self.event_inode,
                        }
                    )

                cycles = self.config.journal_cycles_path
                if cycles.exists():
                    stat = cycles.stat()
                    if (
                        stat.st_mtime_ns != self.last_cycles_mtime_ns
                        and time.monotonic() - self.last_cycles_emit_mono
                        >= self.config.cycles_snapshot_interval
                    ):
                        payload = await asyncio.to_thread(orjson.loads, cycles.read_bytes())
                        snapshot, fingerprints = compact_cycles_delta(
                            payload, self.cycle_fingerprints,
                        )
                        if (
                            snapshot['changed_cycles']
                            or snapshot['removed_cycle_ids']
                            or not self.cycle_fingerprints
                        ):
                            self.emit(
                                'bot_cycles_snapshot', snapshot,
                                event_time_ms=stat.st_mtime_ns // 1_000_000,
                            )
                            self.last_cycles_emit_mono = time.monotonic()
                        self.cycle_fingerprints = fingerprints
                        self.last_cycles_mtime_ns = stat.st_mtime_ns
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Market recording must survive a malformed/missing bot journal.
                if self.health is not None:
                    self.health.error('decision_tap', exc)
            await asyncio.sleep(self.config.decision_poll_interval)
