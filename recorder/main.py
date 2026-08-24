"""Entrypoint for the SMC2026 read-only market recorder."""

import asyncio
import logging
import signal
from collections import OrderedDict, deque

from loi_he_thong.runtime_lock import DuplicateInstanceError, acquire_runtime_lock
from recorder.collector import BinanceRecorder
from recorder.config import RecorderConfig
from recorder.decision_tap import DecisionTap
from recorder.decision_outcomes import DecisionOutcomeTracker
from recorder.health import HealthState, health_loop
from recorder.features import FeatureEngine
from recorder.metadata import code_version, config_version
from recorder.storage import AppendOnlyStore
from recorder.wavefront import WavefrontShadowEvaluator


_FR_STREAMS = {
    'binance_spot_trade_100ms': 'binance_spot',
    'coinbase_spot_trade_100ms': 'coinbase_spot',
    'futures_trade_100ms': 'futures',
}
_FR_WINDOWS = (100, 250, 500, 800)
_FR_FOLLOWUP_MS = 60_000
_FR_LABELS = (
    'IMPACT_CONTINUATION', 'ABSORBED_BUILD', 'FLOW_FAILURE',
    'FULLY_PROPAGATED/CHASE',
)


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class RecorderResearchTracker:
    """Bounded recorder-only FAILED_REVERSION measurements; no live authority."""

    def __init__(self, emit, outcomes):
        self.emit, self.outcomes = emit, outcomes
        self.rows = {venue: deque(maxlen=2048) for venue in _FR_STREAMS.values()}
        self.oi = deque(maxlen=128)
        self.pending = OrderedDict()
        self.closed = deque(maxlen=512)

    @staticmethod
    def _clock(record):
        return int(record.get('receive_time_ms') or record.get('event_time_ms') or 0)

    @classmethod
    def _failed(cls, node, depth=0):
        if not isinstance(node, dict) or depth > 6:
            return None
        if (
            str(node.get('proof_type') or '').upper() == 'FAILED_REVERSION'
            or str(node.get('reason') or '').upper() == 'IGNITION_FAILED_REVERSION'
        ):
            return node
        for key in (
            'ignition', 'output', 'decision_record', 'entry',
            'canonical_opportunity', 'entry_decision', 'causal',
        ):
            found = cls._failed(node.get(key), depth + 1)
            if found:
                return found
        return None

    @staticmethod
    def _pick(nodes, key):
        return next(
            (node[key] for node in nodes if isinstance(node, dict) and node.get(key) is not None),
            None,
        )

    def _register(self, record):
        if record.get('stream') != 'bot_event':
            return
        payload = record.get('payload') or {}
        failed = self._failed(payload)
        if not failed:
            return
        decision = payload.get('decision_record') or {}
        output = decision.get('output') or {}
        ignition = output.get('ignition') or {}
        nodes = (failed, ignition, output, decision, payload)
        episode_id = str(self._pick(nodes, 'causal_episode_id') or '')
        side = str(self._pick(nodes, 'side') or '').upper()
        proposer = str(self._pick(nodes, 'proposer') or '')
        if not episode_id or side not in ('LONG', 'SHORT'):
            return
        try:
            origin = int(episode_id.rsplit(':', 1)[-1])
        except ValueError:
            origin = 0
        if episode_id in self.pending or episode_id in self.closed:
            return
        episode = {
            'causal_episode_id': episode_id, 'origin_ms': origin, 'side': side,
            'proposer': proposer, 'bot_event': str(payload.get('event') or ''),
        }
        if origin <= 0:
            self.emit('failed_reversion_measurement', {
                'version': 'FAILED_REVERSION_MEASUREMENT_V1',
                'authority': False, 'live_gate': False, 'valid': False,
                'invalid_reason': 'CAUSAL_EPISODE_ID_UNPARSEABLE', **episode,
            }, event_time_ms=int(record.get('event_time_ms') or 0))
            self.closed.append(episode_id)
            return
        self.pending[episode_id] = episode
        while len(self.pending) > 64:
            dropped_id, dropped = self.pending.popitem(last=False)
            self.closed.append(dropped_id)
            self.emit('failed_reversion_measurement', {
                'version': 'FAILED_REVERSION_MEASUREMENT_V1',
                'authority': False, 'live_gate': False, 'valid': False,
                'invalid_reason': 'PENDING_CAPACITY_EXCEEDED', **dropped,
            }, event_time_ms=self._clock(record))

    def _trade(self, record, venue):
        payload = record.get('payload') or {}
        first, last = _num(payload.get('first_price')), _num(payload.get('last_price'))
        if first is None or last is None:
            return
        start = int(
            payload.get('bucket_start_ms')
            or (int(record.get('event_time_ms') or 0) // 100) * 100
        )
        self.rows[venue].append((
            start,
            int(payload.get('last_event_time_ms') or record.get('event_time_ms') or start + 99),
            first, last,
            _num(payload.get('high'), max(first, last)),
            _num(payload.get('low'), min(first, last)),
            max(0.0, _num(payload.get('buy_qty'), 0.0)),
            max(0.0, _num(payload.get('sell_qty'), 0.0)),
        ))

    def _open_interest(self, record):
        payload = record.get('payload') or {}
        value = _num(payload.get('openInterest', payload.get('open_interest')))
        when = int(record.get('event_time_ms') or 0)
        if value and value > 0.0 and when > 0 and (not self.oi or when >= self.oi[-1][0]):
            self.oi.append((when, value))

    @staticmethod
    def _venue(rows, origin, side):
        direction = 1.0 if side == 'LONG' else -1.0
        previous = [row for row in rows if row[0] < origin and row[3] > 0.0]
        if not previous:
            return {'anchor': None, 'windows': {}, 'reversion_depth_bps': None, 'reclaim_bps': None}
        anchor_row, anchor = previous[-1], previous[-1][3]
        result = {
            'anchor': {
                'price': anchor, 'event_time_ms': anchor_row[1],
                'age_ms': max(0, origin - anchor_row[1]),
                'method': 'LAST_100MS_TRADE_BEFORE_EPISODE',
            },
            'windows': {},
        }
        for window in _FR_WINDOWS:
            # Keep only complete 100-ms buckets: 250 ms has 200 ms effective coverage.
            selected = [
                row for row in rows
                if origin <= row[0] and row[0] + 100 <= origin + window
            ]
            if not selected:
                continue
            close_bps = direction * (selected[-1][3] - anchor) / anchor * 10_000.0
            favorable_price = (
                max(row[4] for row in selected) if direction > 0
                else min(row[5] for row in selected)
            )
            favorable_bps = max(
                0.0, direction * (favorable_price - anchor) / anchor * 10_000.0
            )
            buy, sell = sum(row[6] for row in selected), sum(row[7] for row in selected)
            net = direction * (buy - sell)
            result['windows'][str(window)] = {
                'signed_close_move_bps': round(close_bps, 6),
                'favorable_excursion_bps': round(favorable_bps, 6),
                'directional_net_flow_btc': round(net, 8),
                'total_volume_btc': round(buy + sell, 8),
                'impact_efficiency_bps_per_net_btc': (
                    round(favorable_bps / net, 6) if net > 0.0 else None
                ),
                'acceptance_ratio': (
                    round(close_bps / favorable_bps, 6) if favorable_bps > 0.0 else None
                ),
            }
        selected = [row for row in rows if origin <= row[0] < origin + 800]
        reversion = reclaim = None
        if selected:
            if direction > 0:
                index = max(range(len(selected)), key=lambda i: selected[i][4])
                peak = selected[index][4]
                reverted = min([selected[index][3]] + [row[5] for row in selected[index + 1:]])
                reversion = (peak - reverted) / anchor * 10_000.0
                reclaim = (selected[-1][3] - reverted) / anchor * 10_000.0
            else:
                index = min(range(len(selected)), key=lambda i: selected[i][5])
                peak = selected[index][5]
                reverted = max([selected[index][3]] + [row[4] for row in selected[index + 1:]])
                reversion = (reverted - peak) / anchor * 10_000.0
                reclaim = (reverted - selected[-1][3]) / anchor * 10_000.0
        result['reversion_depth_bps'] = round(max(0.0, reversion), 6) if reversion is not None else None
        result['reclaim_bps'] = round(max(0.0, reclaim), 6) if reclaim is not None else None
        return result

    def _oi_metrics(self, origin):
        pre = [sample for sample in self.oi if sample[0] <= origin]
        post = [sample for sample in self.oi if origin < sample[0] <= origin + _FR_FOLLOWUP_MS]
        if not pre or not post:
            return None, None, 'INSUFFICIENT_DATA', None
        baseline, followup = pre[-1], post[-1]
        dt = max((followup[0] - baseline[0]) / 1000.0, 1e-9)
        change = (followup[1] - baseline[1]) / baseline[1] * 100.0
        slope = change / dt
        before = None
        if len(pre) >= 2:
            old = pre[-2]
            before = (baseline[1] - old[1]) / old[1] * 100.0 / max(
                (baseline[0] - old[0]) / 1000.0, 1e-9
            )
        acceleration = (slope - before) / dt if before is not None else None
        regime = lambda value: (
            'INSUFFICIENT' if value is None else 'BUILD' if value > 0.0
            else 'UNWIND' if value < 0.0 else 'FLAT'
        )
        return (
            round(slope, 9),
            round(acceleration, 12) if acceleration is not None else None,
            f'{regime(before)}->{regime(slope)}',
            round(change, 9),
        )

    @staticmethod
    def _value(measurement, window, key):
        row = (measurement or {}).get('windows', {}).get(str(window))
        return row.get(key) if isinstance(row, dict) else None

    def _measurement(self, episode):
        venues = {
            venue: self._venue(tuple(self.rows[venue]), episode['origin_ms'], episode['side'])
            for venue in ('binance_spot', 'coinbase_spot', 'futures')
        }
        reference = next((
            venue for venue in (episode.get('proposer'), 'binance_spot', 'coinbase_spot')
            if venue in ('binance_spot', 'coinbase_spot')
            and venues[venue].get('anchor') is not None
            and venues[venue].get('windows', {}).get('800')
        ), None)
        cash = venues.get(reference) if reference else None
        future = venues['futures']
        moves = {
            venue: self._value(measurement, 800, 'signed_close_move_bps')
            for venue, measurement in venues.items()
        }
        cash_move, future_move = moves.get(reference), moves.get('futures')
        gap = (
            cash_move - future_move
            if cash_move is not None and future_move is not None else None
        )
        catchup = (
            future_move / cash_move
            if gap is not None and abs(cash_move) > 1e-12 else None
        )
        oi_slope, oi_accel, oi_transition, oi_change = self._oi_metrics(episode['origin_ms'])
        output = {
            'version': 'FAILED_REVERSION_MEASUREMENT_V1',
            'authority': False, 'live_gate': False,
            'valid': bool(reference and self._value(cash, 800, 'signed_close_move_bps') is not None),
            **episode,
            'reference_venue': reference,
            'anchor_policy': 'PER_VENUE_LAST_100MS_TRADE_BEFORE_EPISODE',
            'venue_moves_bps': moves,
            'venue_measurements': venues,
            'reversion_depth_bps': cash.get('reversion_depth_bps') if cash else None,
            'reclaim_bps': cash.get('reclaim_bps') if cash else None,
            'futures_catchup_ratio': round(catchup, 6) if catchup is not None else None,
            'propagation_remaining_bps': round(max(0.0, gap), 6) if gap is not None else None,
            'oi_slope': oi_slope, 'oi_acceleration': oi_accel,
            'oi_regime_transition': oi_transition, 'oi_change_pct': oi_change,
            'taxonomy': {
                'version': 'FAILED_REVERSION_RESEARCH_V1',
                'authority': False, 'live_gate': False, 'thresholds_calibrated': False,
                'label': 'PENDING_CALIBRATION', 'candidate_labels': list(_FR_LABELS),
                'minimum_samples_before_threshold_review': 20,
                'target_sample_range': [20, 30],
            },
            'measurement_notes': {
                'impact_efficiency_units': 'bps_per_directional_net_btc',
                'acceptance_definition': 'signed_close_move_bps/favorable_excursion_bps',
                'window_quantization': 'COMPLETE_100MS_BUCKETS_ONLY_NO_LOOKAHEAD',
                'requested_250ms_effective_coverage_ms': 200,
                'oi_followup_ms': _FR_FOLLOWUP_MS, 'new_data_source': False,
            },
        }
        output['invalid_reason'] = None if output['valid'] else 'REFERENCE_CASH_800MS_INCOMPLETE'
        for window in (100, 250, 500):
            output[f'impact_efficiency_{window}ms'] = self._value(
                cash, window, 'impact_efficiency_bps_per_net_btc'
            )
        for window in _FR_WINDOWS:
            output[f'acceptance_{window}ms'] = self._value(cash, window, 'acceptance_ratio')
        return output

    def _finalize(self, now):
        due = [
            episode_id for episode_id, episode in self.pending.items()
            if now >= episode['origin_ms'] + _FR_FOLLOWUP_MS
        ]
        for episode_id in due:
            episode = self.pending.pop(episode_id)
            self.closed.append(episode_id)
            self.emit(
                'failed_reversion_measurement', self._measurement(episode),
                event_time_ms=episode['origin_ms'] + _FR_FOLLOWUP_MS,
            )

    def observe(self, record):
        self.outcomes.observe(record)
        stream = str(record.get('stream') or '')
        venue = _FR_STREAMS.get(stream)
        if venue:
            self._trade(record, venue)
        elif stream == 'open_interest':
            self._open_interest(record)
        elif stream == 'bot_event':
            self._register(record)
        now = self._clock(record)
        if now > 0 and self.pending:
            self._finalize(now)

async def run():
    config = RecorderConfig()
    config.data_root.mkdir(parents=True, exist_ok=True)
    health = HealthState(config)
    store = AppendOnlyStore(config, health)
    retention = await store.prune_once()
    code_id = code_version()
    config_id = config_version(config)
    health.code_version = code_id
    health.config_version = config_id
    feature_engine = FeatureEngine(
        config, store.publish, health, code_id, config_id
    )
    collector = BinanceRecorder(
        config, store, health, feature_engine, code_id, config_id
    )
    research_emit = lambda stream, payload, event_time_ms=None: collector.emit(
        stream, payload, event_time_ms=event_time_ms,
        source='wstrade_recorder', feed_features=False,
    )
    collector.decision_outcome_tracker = RecorderResearchTracker(
        research_emit, DecisionOutcomeTracker(research_emit)
    )
    if config.wavefront_enabled:
        collector.wavefront_evaluator = WavefrontShadowEvaluator(
            lambda stream, payload, event_time_ms=None: collector.emit(
                stream, payload, event_time_ms=event_time_ms,
                source='wstrade_wavefront_shadow', feed_features=False,
            ),
            runtime_health_path=config.bot_runtime_path,
            cpu_status_path=config.cpu_status_path,
            feed_health=lambda: dict(health.connections),
            state_path=(
                config.data_root / 'derived' / 'wavefront'
                / f'{code_id}-{config_id}.json'
            ),
            evidence_version=f'{code_id}:{config_id}',
        )
    tap = DecisionTap(config, collector.emit, health)
    await collector.start_session()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    writer = asyncio.create_task(store.writer_loop(), name='writer')
    tasks = [
        asyncio.create_task(store.compactor_loop(), name='compactor'),
        asyncio.create_task(store.retention_loop(), name='retention'),
        asyncio.create_task(health_loop(health), name='health'),
        asyncio.create_task(collector.public_loop(), name='public_ws'),
        asyncio.create_task(collector.market_loop(), name='market_ws'),
        asyncio.create_task(collector.binance_spot_loop(), name='binance_spot_ws'),
        asyncio.create_task(collector.coinbase_spot_loop(), name='coinbase_spot_ws'),
        asyncio.create_task(collector.macro_poll_loop(), name='macro_rest'),
        asyncio.create_task(tap.loop(), name='decision_tap'),
    ]
    logging.info(
        '[RECORDER] started symbol=%s root=%s public-only=true retention=%sh '
        'startup_deleted=%s startup_freed_bytes=%s',
        config.symbol, config.data_root, config.retention_hours,
        retention['files_deleted'], retention['bytes_deleted'],
    )
    try:
        await stop.wait()
    finally:
        # asyncio cancellation cannot stop a function already running through
        # to_thread. Signal compact_wal first so it exits at its next bounded
        # chunk instead of holding systemd shutdown until SIGKILL.
        store.request_shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        feature_engine.close()
        try:
            await asyncio.wait_for(store.stop_writer(), timeout=5.0)
        except asyncio.TimeoutError:
            health.error('shutdown', 'QUEUE_DRAIN_TIMEOUT')
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        await collector.close_session()
        logging.info('[RECORDER] stopped cleanly')


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    try:
        runtime_lock = acquire_runtime_lock('recorder')
    except DuplicateInstanceError as exc:
        logging.critical('[RECORDER] %s', exc)
        raise SystemExit(73) from exc
    try:
        asyncio.run(run())
    finally:
        runtime_lock.close()


if __name__ == '__main__':
    main()
