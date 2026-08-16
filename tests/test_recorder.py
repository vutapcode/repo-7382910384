import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import orjson
import pyarrow.parquet as pq

from recorder.config import RecorderConfig
from recorder.depth import DepthGap, LocalOrderBook
from recorder.decision_tap import compact_cycles_delta
from recorder.features import FeatureEngine
from recorder.health import HealthState
from recorder.replay import DeterministicReplay
from recorder.storage import (
    AppendOnlyStore, compact_wal, prune_expired_partitions, wal_path,
)


class DepthReplayTests(unittest.TestCase):
    def test_snapshot_overlap_and_continuity(self):
        book = LocalOrderBook()
        book.reset({
            'lastUpdateId': 100,
            'bids': [['99', '2']],
            'asks': [['101', '3']],
        })
        self.assertEqual(
            book.apply({
                'U': 99, 'u': 101, 'pu': 98,
                'b': [['99', '0'], ['98', '4']], 'a': [['101', '5']],
            }),
            'APPLIED',
        )
        self.assertTrue(book.synced)
        self.assertNotIn('99', book.bids)
        self.assertEqual(book.asks['101'], '5')
        self.assertEqual(
            book.apply({
                'U': 102, 'u': 103, 'pu': 101,
                'b': [['98', '6']], 'a': [],
            }),
            'APPLIED',
        )
        self.assertEqual(book.last_u, 103)
        ticker = book.best_ticker()
        self.assertEqual(ticker['b'], '98')
        self.assertEqual(ticker['a'], '101')
        self.assertTrue(ticker['derived_from_depth'])

    def test_gap_is_never_silent(self):
        book = LocalOrderBook()
        book.reset({'lastUpdateId': 100, 'bids': [], 'asks': []})
        book.apply({'U': 100, 'u': 101, 'pu': 99, 'b': [], 'a': []})
        with self.assertRaises(DepthGap):
            book.apply({'U': 105, 'u': 106, 'pu': 104, 'b': [], 'a': []})

    def test_stale_buffered_event_is_discarded(self):
        book = LocalOrderBook()
        book.reset({'lastUpdateId': 100, 'bids': [], 'asks': []})
        self.assertEqual(
            book.apply({'U': 90, 'u': 99, 'pu': 89, 'b': [], 'a': []}),
            'STALE',
        )

    def test_checkpoint_resumes_with_next_pu(self):
        book = LocalOrderBook()
        book.reset_checkpoint({
            'lastUpdateId': 200, 'snapshotUpdateId': 100,
            'bids': [['99', '2']], 'asks': [['101', '3']],
        })
        self.assertEqual(
            book.apply({
                'U': 201, 'u': 202, 'pu': 200,
                'b': [['99', '4']], 'a': [],
            }),
            'APPLIED',
        )
        self.assertEqual(book.last_u, 202)

    def test_microstructure_bands(self):
        book = LocalOrderBook()
        book.reset_checkpoint({
            'lastUpdateId': 1,
            'bids': [['99.99', '2'], ['99.90', '3']],
            'asks': [['100.01', '1'], ['100.10', '4']],
        })
        metrics = book.microstructure()
        self.assertAlmostEqual(metrics['mid'], 100.0)
        self.assertGreater(metrics['obi_5bps'], 0.0)


class FeatureTests(unittest.TestCase):
    def test_depth_feature_count_uses_lightweight_path(self):
        config = replace(RecorderConfig(), feature_lateness_seconds=1)
        health = HealthState(config)
        output = []
        engine = FeatureEngine(config, output.append, health, 'code', 'config')
        engine.observe_depth_diff({
            'stream': 'depth_diff', 'event_time_ms': 1100,
            'receive_time_ms': 1101, 'payload': {'large': ['ignored'] * 100},
        })
        engine.process({
            'stream': 'book_ticker', 'event_time_ms': 3100,
            'receive_time_ms': 3101, 'payload': {'b': '99', 'a': '101'},
        })
        feature = next(row for row in output if row['sequence_end'] == 1)
        self.assertEqual(feature['payload']['event_counts']['depth_diff'], 1)
        self.assertEqual(feature['payload']['delay_samples'], 1)

    def test_health_keeps_error_history_but_marks_component_recovered(self):
        health = HealthState(RecorderConfig())
        health.connection('rest_macro', False)
        health.error('rest_macro', 'temporary timeout')
        failed = health.snapshot()
        self.assertEqual(failed['current_status'], 'ERROR')
        self.assertEqual(failed['consecutive_errors'], 1)

        health.connection('rest_macro', True)
        recovered = health.snapshot()
        self.assertEqual(recovered['current_status'], 'OK')
        self.assertEqual(recovered['consecutive_errors'], 0)
        self.assertIsNotNone(recovered['recovered_at_ms'])
        self.assertEqual(
            recovered['last_error']['message'], 'temporary timeout'
        )

    def test_one_second_features_include_flow_book_and_liquidation(self):
        config = replace(RecorderConfig(), feature_lateness_seconds=1)
        health = HealthState(config)
        output = []
        engine = FeatureEngine(config, output.append, health, 'code', 'config')
        book = LocalOrderBook()
        book.reset_checkpoint({
            'lastUpdateId': 1,
            'bids': [['99.99', '2']], 'asks': [['100.01', '1']],
        })
        engine.capture_book(1, book)

        def record(stream, event_ms, payload):
            return {
                'stream': stream, 'event_time_ms': event_ms,
                'receive_time_ms': event_ms + 5, 'payload': payload,
            }

        engine.process(record('agg_trade', 1100, {'p': '100', 'q': '2', 'm': False}))
        engine.process(record('book_ticker', 1200, {
            'b': '99.99', 'B': '2', 'a': '100.01', 'A': '1',
        }))
        engine.process(record('liquidation', 1300, {
            'o': {'S': 'SELL', 'z': '0.5', 'ap': '100'},
        }))
        engine.process(record('mark_price', 3100, {'markPrice': '101'}))
        feature = next(row for row in output if row['sequence_end'] == 1)
        payload = feature['payload']
        self.assertEqual(payload['buy_qty'], 2.0)
        self.assertEqual(payload['long_liquidation_quote'], 50.0)
        self.assertIsNotNone(payload['book'])
        self.assertEqual(feature['code_version'], 'code')

    def test_late_macro_is_forward_filled_without_reopening_old_bucket(self):
        config = replace(RecorderConfig(), feature_lateness_seconds=1, oi_interval=30)
        health = HealthState(config)
        output = []
        engine = FeatureEngine(config, output.append, health, 'code', 'config')

        def record(stream, event_ms, payload, receive_ms=None):
            return {
                'stream': stream,
                'event_time_ms': event_ms,
                'receive_time_ms': receive_ms or event_ms,
                'payload': payload,
            }

        engine.process(record('agg_trade', 1100, {'p': '100', 'q': '1', 'm': False}))
        engine.process(record('agg_trade', 3100, {'p': '101', 'q': '1', 'm': False}))
        self.assertEqual(engine.flushed_through, 1)

        # REST OI is older than the already-emitted watermark. It must still
        # become available to later features, without creating a duplicate s=1.
        engine.process(record(
            'open_interest', 1500, {'openInterest': '250'}, receive_ms=7200,
        ))
        engine.process(record('agg_trade', 4100, {'p': '102', 'q': '1', 'm': False}))
        feature = next(row for row in output if row['sequence_end'] == 3)
        self.assertEqual(feature['payload']['macro']['open_interest'], 250.0)
        self.assertEqual(feature['payload']['macro_age_ms']['open_interest'], 2499)
        self.assertTrue(feature['payload']['macro_fresh']['open_interest'])
        self.assertEqual(
            health.sampled_out['feature_event_too_late.open_interest'], 1
        )
        self.assertEqual(
            health.sampled_out['feature_macro_forward_fill.open_interest'], 1
        )
        late = health.snapshot()['late_events']
        self.assertEqual(late['count_total'], 1)
        self.assertEqual(late['count_by_source']['open_interest'], 1)
        self.assertEqual(late['delay_ms']['p95'], 5700)
        self.assertEqual(
            len([row for row in output if row['sequence_end'] == 1]), 1
        )

    def test_feature_identity_and_rolling_cvd_are_restart_safe_metadata(self):
        config = replace(RecorderConfig(), feature_lateness_seconds=0)
        health = HealthState(config)
        output = []
        engine = FeatureEngine(config, output.append, health, 'code', 'config')
        engine.process({
            'stream': 'agg_trade', 'event_time_ms': 1100,
            'receive_time_ms': 1101,
            'payload': {'p': '100', 'q': '2', 'm': False},
        })
        feature = output[0]['payload']
        self.assertEqual(feature['feature_identity'], f'{config.symbol}:1')
        self.assertEqual(feature['recorder_run_id'], engine.recorder_run_id)
        self.assertEqual(feature['cvd_btc_60s'], 2.0)

    def test_websocket_mark_price_compact_fields_are_emitted_fresh(self):
        config = replace(RecorderConfig(), feature_lateness_seconds=0)
        health = HealthState(config)
        output = []
        engine = FeatureEngine(config, output.append, health, 'code', 'config')
        engine.process({
            'stream': 'mark_price', 'event_time_ms': 1100,
            'receive_time_ms': 1105,
            'payload': {'p': '101.5', 'i': '101.4', 'r': '0.0001'},
        })
        macro = output[0]['payload']['macro']
        self.assertEqual(macro['mark_price'], 101.5)
        self.assertEqual(macro['index_price'], 101.4)
        self.assertEqual(macro['funding_rate'], 0.0001)
        self.assertEqual(output[0]['payload']['macro_age_ms']['mark_price'], 899)


class ReplayTests(unittest.TestCase):
    @staticmethod
    def records():
        base = {
            'schema_version': 2, 'code_version': 'c', 'config_version': 'x',
            'source': 'test', 'symbol': 'BTCUSDT',
            'sequence_start': None, 'sequence_end': None,
            'previous_sequence': None,
        }
        rows = [
            dict(base, stream='depth_checkpoint', event_time_ms=1000,
                 receive_time_ms=1000, payload={
                     'lastUpdateId': 10, 'bids': [['99', '2']],
                     'asks': [['101', '3']],
                 }),
            dict(base, stream='depth_diff', event_time_ms=1100,
                 receive_time_ms=1101, payload={
                     'U': 11, 'u': 12, 'pu': 10,
                     'b': [['99', '4']], 'a': [],
                 }),
            dict(base, stream='agg_trade', event_time_ms=1200,
                 receive_time_ms=1201, payload={'p': '100', 'q': '2', 'm': False}),
            dict(base, stream='liquidation', event_time_ms=1300,
                 receive_time_ms=1301, payload={
                     'o': {'S': 'SELL', 'z': '0.5', 'ap': '100'},
                 }),
            dict(base, stream='bot_event', event_time_ms=1400,
                 receive_time_ms=1401, payload={
                     'event': 'DECISION_EVALUATED',
                     'payload': {'setup_id': 's1', 'result': 'CORE_REJECT'},
                 }),
        ]
        return rows

    def test_replay_is_repeatable_and_rebuilds_depth(self):
        first = DeterministicReplay(setup_id='s1').run(self.records())
        second = DeterministicReplay(setup_id='s1').run(self.records())
        self.assertEqual(first, second)
        self.assertEqual(first['depth_gaps'], 0)
        self.assertEqual(first['depth_last_u'], 12)
        self.assertEqual(first['buy_qty'], 2.0)
        self.assertEqual(first['long_liquidation_quote'], 50.0)
        self.assertEqual(first['decision_results']['CORE_REJECT'], 1)
        self.assertEqual(len(first['timeline']), 1)


class StorageTests(unittest.TestCase):
    def test_cycle_snapshot_is_compact_delta_and_dedupes_unchanged_journal(self):
        large_history = [{'event_id': f'e{i}', 'blob': 'x' * 1000} for i in range(50)]
        payload = {
            'schema_version': 2,
            'updated_at': 10.0,
            'cycles': [{
                'position_cycle_id': 'pc1', 'setup_id': 's1', 'status': 'OPEN',
                'bias': 'LONG', 'score_v2_history': large_history,
                'actual': {'orders': large_history, 'net_pnl_quote': None},
            }],
            'continuous_shadow_registry': {'huge': large_history},
        }
        first, fingerprints = compact_cycles_delta(payload)
        self.assertEqual(len(first['changed_cycles']), 1)
        self.assertNotIn('score_v2_history', first['changed_cycles'][0])
        self.assertNotIn('orders', first['changed_cycles'][0]['actual'])
        self.assertLess(len(orjson.dumps(first)), len(orjson.dumps(payload)) // 10)

        payload['updated_at'] = 11.0
        second, fingerprints = compact_cycles_delta(payload, fingerprints)
        self.assertEqual(second['changed_cycles'], [])
        self.assertEqual(second['removed_cycle_ids'], [])

        payload['cycles'][0]['status'] = 'CLOSED'
        third, _ = compact_cycles_delta(payload, fingerprints)
        self.assertEqual(len(third['changed_cycles']), 1)
        self.assertEqual(third['changed_cycles'][0]['status'], 'CLOSED')

    def test_retention_prunes_strictly_older_raw_partitions_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = {
                'expired_wal': root / 'raw/wal/agg_trade/2026-08-10/00.jsonl',
                'kept_wal': root / 'raw/wal/agg_trade/2026-08-10/01.jsonl',
                'expired_parquet': root / 'raw/parquet/agg_trade/2026-08-09/23.parquet',
                'derived': root / 'derived/replay_baselines/2026-08-09.jsonl',
                'unrelated': root / 'raw/wal/agg_trade/2026-08-09/notes.txt',
            }
            for path in paths.values():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'1234')
            result = prune_expired_partitions(
                root, retention_hours=24,
                now=datetime(2026, 8, 11, 0, 30, tzinfo=timezone.utc),
            )
            self.assertEqual(result['files_deleted'], 2)
            self.assertEqual(result['bytes_deleted'], 8)
            self.assertFalse(paths['expired_wal'].exists())
            self.assertFalse(paths['expired_parquet'].exists())
            self.assertTrue(paths['kept_wal'].exists())
            self.assertTrue(paths['derived'].exists())
            self.assertTrue(paths['unrelated'].exists())

    def test_retention_rejects_broad_or_nonpositive_targets(self):
        with self.assertRaises(ValueError):
            prune_expired_partitions(Path('/home/ubuntu'), 24)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                prune_expired_partitions(Path(temp), 0)

    def test_compactor_salvages_complete_record_after_torn_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wal = root / 'raw/wal/agg_trade/2026-08-10/08.jsonl'
            wal.parent.mkdir(parents=True)
            record = {
                'schema_version': 2, 'code_version': 'c', 'config_version': 'x',
                'source': 'test', 'symbol': 'BTCUSDT', 'stream': 'agg_trade',
                'event_time_ms': 1, 'receive_time_ms': 1,
                'sequence_start': 1, 'sequence_end': 1,
                'previous_sequence': None, 'payload': {'p': '100'},
            }
            good = orjson.dumps(record)
            wal.write_bytes(b'{"schema_version":2,"code_versi' + good + b'\n')
            target, recovered, dropped = compact_wal(
                wal, root, return_stats=True,
            )
            self.assertTrue(target.exists())
            self.assertEqual(recovered, 1)
            self.assertEqual(dropped, 0)
            self.assertEqual(pq.read_table(target).num_rows, 1)

    def test_compactor_flushes_oversized_payloads_by_byte_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wal = root / 'raw/wal/bot_cycles_snapshot/2026-08-10/09.jsonl'
            wal.parent.mkdir(parents=True)
            rows = []
            for index in range(5):
                record = {
                    'schema_version': 2, 'code_version': 'c',
                    'config_version': 'x', 'source': 'test',
                    'symbol': 'BTCUSDT', 'stream': 'bot_cycles_snapshot',
                    'event_time_ms': index, 'receive_time_ms': index,
                    'sequence_start': None, 'sequence_end': None,
                    'previous_sequence': None,
                    'payload': {'blob': 'x' * 4096, 'index': index},
                }
                rows.append(orjson.dumps(record) + b'\n')
            wal.write_bytes(b''.join(rows))
            target = compact_wal(
                wal, root, chunk_size=100_000, chunk_bytes=5_000,
            )
            parquet = pq.ParquetFile(target)
            self.assertEqual(parquet.metadata.num_rows, 5)
            self.assertGreaterEqual(parquet.metadata.num_row_groups, 3)

    def test_bounded_queue_wal_and_parquet(self):
        # asyncio.run avoids a Python 3.10 IsolatedAsyncioTestCase teardown bug
        # after asyncio.to_thread has been used by the writer.
        asyncio.run(self._exercise_bounded_queue_wal_and_parquet())

    async def _exercise_bounded_queue_wal_and_parquet(self):
        async def inline_to_thread(func, /, *args, **kwargs):
            # Keep this unit test deterministic on Python 3.10, whose default
            # executor shutdown is flaky in the sandbox. _write_batch itself is
            # still exercised; production retains asyncio.to_thread.
            return func(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            config = replace(
                RecorderConfig(), data_root=Path(temp), queue_max=2,
                batch_max=10, flush_interval=0.01,
            )
            health = HealthState(config)
            store = AppendOnlyStore(config, health)
            record = {
                'schema_version': 1, 'source': 'test', 'symbol': 'BTCUSDT',
                'stream': 'agg_trade', 'event_time_ms': 1,
                'receive_time_ms': 1, 'sequence_start': 1,
                'sequence_end': 1, 'previous_sequence': None,
                'payload': {'p': '100'},
            }
            self.assertTrue(store.publish(record))
            self.assertTrue(store.publish(dict(record, sequence_end=2)))
            self.assertFalse(store.publish(dict(record, sequence_end=3)))
            self.assertEqual(health.dropped, 1)

            with mock.patch('recorder.storage.asyncio.to_thread', new=inline_to_thread):
                task = asyncio.create_task(store.writer_loop())
                # Give the freshly created writer one scheduling turn before
                # waiting on the queue's completion event.
                await asyncio.sleep(0.05)
                await asyncio.wait_for(store.stop_writer(), timeout=2)
                await asyncio.wait_for(task, timeout=2)
            path = wal_path(config.data_root, record)
            self.assertTrue(path.exists())
            self.assertEqual(len(path.read_text().splitlines()), 2)
            target = compact_wal(path, config.data_root)
            self.assertTrue(target.exists())
            table = pq.read_table(target)
            self.assertEqual(table.num_rows, 2)
            payload = orjson.loads(table['payload_json'][0].as_py())
            self.assertEqual(payload['p'], '100')


if __name__ == '__main__':
    unittest.main()
