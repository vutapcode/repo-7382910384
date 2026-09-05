import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import orjson
import pyarrow.parquet as pq

from recorder.config import RecorderConfig
from recorder.collector import BinanceRecorder
from recorder.cash import (
    CashTradeBatcher, coinbase_time_ms, is_coinbase_live_match,
)
from recorder.depth import DepthGap, LocalOrderBook
from recorder.coinbase_l2 import CoinbaseL2Book, CoinbaseL2UpdateBatcher
from recorder.decision_tap import DecisionTap, compact_cycles_delta
from recorder.features import FeatureEngine
from recorder.health import HealthState
from recorder.liquidity_response import SpotLiquidityResponseAnalyzer
from recorder.replay import DeterministicReplay, iter_merged_records
from recorder.storage import (
    AppendOnlyStore, CompactionCpuDeferred, compact_wal, cpu_allows_compaction,
    prune_expired_partitions, wal_path,
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

    def test_partial_depth_replaces_top20_and_checks_sequence(self):
        book = LocalOrderBook()
        first = {
            'partial': True, 'U': 10, 'u': 12, 'pu': 9,
            'b': [[str(100 - i), '1'] for i in range(25)],
            'a': [[str(101 + i), '2'] for i in range(25)],
        }
        self.assertEqual(book.apply_partial(first), 'APPLIED')
        self.assertEqual(len(book.bids), 20)
        self.assertEqual(len(book.asks), 20)
        self.assertEqual(len(book.checkpoint()['bids']), 20)
        second = {
            'partial': True, 'U': 13, 'u': 14, 'pu': 12,
            'b': [['100', '3']], 'a': [['101', '4']],
        }
        book.apply_partial(second)
        self.assertEqual(book.bids, {'100': '3'})
        with self.assertRaises(DepthGap):
            book.apply_partial({
                'partial': True, 'U': 20, 'u': 21, 'pu': 19,
                'b': [['100', '1']], 'a': [['101', '1']],
            })


class CashRecorderTests(unittest.TestCase):
    def test_recorder_uses_top20_depth_stream(self):
        config = RecorderConfig()
        self.assertIn('@depth20@100ms', config.public_stream_url)
        self.assertIn('@depth5@100ms', config.spot_stream_url)
        self.assertNotIn('@bookTicker', config.spot_stream_url)
        self.assertNotIn('@kline_', config.market_stream_url)

    def test_coinbase_recorder_subscribes_to_matches_ticker_and_level2(self):
        source = Path(__file__).resolve().parents[1] / 'recorder' / 'collector.py'
        text = source.read_text(encoding='utf-8')
        self.assertIn(
            "'channels': ['matches', 'ticker', 'level2_batch']", text
        )
        self.assertIn("elif message_type == 'ticker'", text)
        self.assertIn("'coinbase_spot_ticker'", text)
        self.assertIn("'coinbase_spot_l2batch'", text)

    def test_coinbase_l2_batch_is_one_lossless_record(self):
        rows = []

        class Store:
            @staticmethod
            def publish(record):
                rows.append(record)
                return True

        config = RecorderConfig()
        recorder = BinanceRecorder(config, Store(), HealthState(config))
        book = CoinbaseL2Book()
        book.reset({
            'bids': [['99', '2']], 'asks': [['101', '3']],
        })
        batch = {
            'bucket_start_ms': 1_000, 'bucket_end_ms': 1_099,
            'first_event_time_ms': 1_010, 'last_event_time_ms': 1_080,
            'events': [
                {'event_time_ms': 1_010, 'receive_time_ms': 1_020,
                 'changes': [['buy', '99', '4']]},
                {'event_time_ms': 1_080, 'receive_time_ms': 1_090,
                 'changes': [['sell', '101', '2']]},
            ],
            'update_count': 2, 'change_count': 2,
        }
        with mock.patch.object(recorder, 'now_ms', return_value=1_105):
            recorder._emit_coinbase_l2_batch(batch, book, 'BTC-USD')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['stream'], 'coinbase_spot_l2batch')
        self.assertEqual(rows[0]['receive_time_ms'], 1_105)
        self.assertEqual(rows[0]['payload']['batch_available_time_ms'], 1_105)
        self.assertTrue(rows[0]['payload']['ordered_lossless_changes'])

    def test_closed_l2_bucket_checkpoint_cannot_see_next_bucket_update(self):
        observed = []

        class Store:
            @staticmethod
            def publish(_record):
                return True

        class Probe:
            @staticmethod
            def observe(record):
                if record.get('stream') == 'coinbase_spot_depth20':
                    observed.append(record)

        config = RecorderConfig()
        recorder = BinanceRecorder(config, Store(), HealthState(config))
        recorder.coinbase_liquidity_response_analyzer = Probe()
        book = CoinbaseL2Book()
        book.reset({'bids': [['99', '2']], 'asks': [['101', '3']]})
        batcher = CoinbaseL2UpdateBatcher(100)
        batcher.push(1_010, 1_005, [['buy', '99', '4']])
        book.apply({'changes': [['buy', '99', '4']]})
        completed = batcher.push(1_110, 1_105, [['buy', '100', '7']])
        with mock.patch.object(recorder, 'now_ms', return_value=1_115):
            recorder._emit_coinbase_l2_batch(
                completed, book, 'BTC-USD',
            )
        # The update at 1_110 belongs to the next bucket and has not been
        # applied yet, so the previous bucket checkpoint must still top at 99.
        self.assertEqual(observed[-1]['payload']['bids'][0][0], '99')

    def test_derived_research_record_is_not_fed_back_into_analyzers(self):
        published = []

        class Store:
            @staticmethod
            def publish(record):
                published.append(record)
                return True

        class Probe:
            def __init__(self):
                self.records = []

            def observe(self, record):
                self.records.append(record)

            @staticmethod
            def summary():
                return {}

        config = RecorderConfig()
        recorder = BinanceRecorder(config, Store(), HealthState(config))
        probes = [Probe() for _ in range(4)]
        recorder.decision_outcome_tracker = probes[0]
        recorder.wavefront_evaluator = probes[1]
        recorder.liquidity_response_analyzer = probes[2]
        recorder.spot_liquidity_response_analyzer = probes[3]
        recorder.emit(
            'binance_spot_trade_100ms', {'buy_qty': 1, 'sell_qty': 0},
            event_time_ms=1_000, receive_time_ms=1_001,
        )
        recorder.emit(
            'spot_liquidity_response', {'authority': False},
            event_time_ms=1_500, receive_time_ms=1_501,
            feed_features=False, feed_research=False,
        )
        self.assertEqual(len(published), 2)
        self.assertTrue(all(len(probe.records) == 1 for probe in probes))

    def test_coinbase_trade_and_ticker_coexist_with_valid_wal_timestamps(self):
        published = []
        sent = []

        class Store:
            @staticmethod
            def publish(record):
                published.append(record)
                return True

        class FakeWebSocket:
            def __init__(self):
                self.messages = [
                    {
                        'type': 'match', 'trade_id': 41, 'side': 'sell',
                        'price': '100', 'size': '0.2',
                        'time': '1970-01-01T00:00:01.500Z',
                    },
                    {
                        'type': 'ticker', 'sequence': 42,
                        'product_id': 'BTC-USD', 'price': '100.5',
                        'best_bid': '100.4', 'best_ask': '100.6',
                        'last_size': '0.1',
                        'time': '1970-01-01T00:00:01.600Z',
                    },
                ]

            async def send(self, payload):
                sent.append(orjson.loads(payload))

            async def recv(self):
                if self.messages:
                    return orjson.dumps(self.messages.pop(0))
                raise asyncio.CancelledError

        class Connection:
            def __init__(self, websocket):
                self.websocket = websocket

            async def __aenter__(self):
                return self.websocket

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        config = replace(RecorderConfig(), cash_ticker_interval=0.0)
        recorder = BinanceRecorder(config, Store(), HealthState(config))
        connection = Connection(FakeWebSocket())
        with mock.patch(
            'recorder.collector.websockets.connect', return_value=connection
        ), mock.patch.object(recorder, 'now_ms', return_value=2_000):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(recorder.coinbase_spot_loop())

        self.assertEqual(
            sent[0]['channels'], ['matches', 'ticker', 'level2_batch']
        )
        by_stream = {record['stream']: record for record in published}
        self.assertIn('coinbase_spot_trade_100ms', by_stream)
        self.assertIn('coinbase_spot_ticker', by_stream)
        for record in by_stream.values():
            self.assertGreater(record['event_time_ms'], 0)
            self.assertGreaterEqual(
                record['receive_time_ms'], record['event_time_ms']
            )
        self.assertEqual(
            by_stream['coinbase_spot_ticker']['payload']['bid'], '100.4'
        )

    def test_hot_coinbase_queue_yields_for_shutdown_cancellation(self):
        class Store:
            @staticmethod
            def publish(record):
                return True

        class HotWebSocket:
            def __init__(self):
                self.received = 0
                self.on_first = None

            async def send(self, payload):
                return None

            async def recv(self):
                self.received += 1
                if self.received == 1 and self.on_first is not None:
                    self.on_first()
                if self.received >= 1_000:
                    raise asyncio.CancelledError
                return orjson.dumps({
                    'type': 'ticker', 'sequence': self.received,
                    'product_id': 'BTC-USD', 'price': '100.5',
                    'best_bid': '100.4', 'best_ask': '100.6',
                    'last_size': '0.1',
                    'time': '1970-01-01T00:00:01.600Z',
                })

        class Connection:
            def __init__(self, websocket):
                self.websocket = websocket

            async def __aenter__(self):
                return self.websocket

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        async def exercise():
            task = asyncio.create_task(recorder.coinbase_spot_loop())
            await task

        config = replace(RecorderConfig(), cash_ticker_interval=0.0)
        recorder = BinanceRecorder(config, Store(), HealthState(config))
        websocket = HotWebSocket()
        websocket.on_first = recorder.request_shutdown
        with mock.patch(
            'recorder.collector.websockets.connect',
            return_value=Connection(websocket),
        ):
            asyncio.run(exercise())
        self.assertLess(websocket.received, 1_000)

    def test_oi_polling_is_independent_from_bot_strategy_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bot_runtime.json'
            config = replace(
                RecorderConfig(), bot_runtime_path=path,
                oi_interval=15, oi_pressure_interval=5,
            )
            recorder = BinanceRecorder(config, mock.Mock(), HealthState(config))
            path.write_text(json.dumps({
                'updated_at_ms': 100_000, 'whale_intent_state': 'PRESSURE',
            }))
            with mock.patch.object(recorder, 'now_ms', return_value=105_000):
                self.assertEqual(recorder._oi_poll_interval(), 15)
            with mock.patch.object(recorder, 'now_ms', return_value=116_001):
                self.assertEqual(recorder._oi_poll_interval(), 15)

    def test_macro_rest_calls_keep_independent_receipt_times(self):
        config = RecorderConfig()
        recorder = BinanceRecorder(
            config, mock.Mock(), HealthState(config)
        )
        release_oi = asyncio.Event()
        release_premium = asyncio.Event()

        async def fake_get(path, params=None):
            if path.endswith('/openInterest'):
                await release_oi.wait()
                return {'time': 900, 'openInterest': '10'}
            await release_premium.wait()
            return {'time': 950, 'markPrice': '100'}

        observed_times = iter((1_000, 1_200))
        recorder._get_json = fake_get
        recorder.now_ms = lambda: next(observed_times)

        async def exercise():
            oi_task = asyncio.create_task(recorder._get_json_observed(
                '/fapi/v1/openInterest', {'symbol': 'BTCUSDT'}
            ))
            premium_task = asyncio.create_task(recorder._get_json_observed(
                '/fapi/v1/premiumIndex', {'symbol': 'BTCUSDT'}
            ))
            await asyncio.sleep(0)
            release_oi.set()
            oi = await oi_task
            release_premium.set()
            premium = await premium_task
            return oi, premium

        oi, premium = asyncio.run(exercise())
        self.assertEqual(oi[1], 1_000)
        self.assertEqual(premium[1], 1_200)
        self.assertEqual(oi[0]['openInterest'], '10')
        self.assertEqual(premium[0]['markPrice'], '100')

    def test_micro_batch_preserves_exact_side_totals(self):
        batcher = CashTradeBatcher(100)
        self.assertIsNone(batcher.push(
            receive_time_ms=1_001, event_time_ms=900, trade_id=10,
            price='100', qty='2', aggressive_buy=True,
        ))
        self.assertIsNone(batcher.push(
            receive_time_ms=1_099, event_time_ms=950, trade_id=11,
            price='101', qty='3', aggressive_buy=False,
        ))
        completed = batcher.push(
            receive_time_ms=1_100, event_time_ms=1_000, trade_id=12,
            price='102', qty='4', aggressive_buy=True,
        )
        self.assertEqual(completed['trade_count'], 2)
        self.assertEqual(completed['buy_qty'], 2.0)
        self.assertEqual(completed['sell_qty'], 3.0)
        self.assertEqual(completed['buy_quote'], 200.0)
        self.assertEqual(completed['sell_quote'], 303.0)
        self.assertEqual(completed['first_trade_id'], 10)
        self.assertEqual(completed['last_trade_id'], 11)
        self.assertEqual(batcher.flush()['buy_qty'], 4.0)

    def test_quiet_batch_flushes_at_receive_deadline(self):
        batcher = CashTradeBatcher(100)
        batcher.push(
            receive_time_ms=2_010, event_time_ms=2_000, trade_id=20,
            price=100, qty=1, aggressive_buy=False,
        )
        self.assertIsNone(batcher.flush_due(2_099))
        self.assertEqual(batcher.flush_due(2_100)['sell_qty'], 1.0)
        self.assertIsNone(batcher.flush_due(2_200))

    def test_futures_q_nq_is_recorded_without_fabricating_missing_nq(self):
        batcher = CashTradeBatcher(100, track_nq=True)
        batcher.push(
            receive_time_ms=3_001, event_time_ms=3_000, trade_id=30,
            price=100, qty=2, non_rpi_qty=1.25, aggressive_buy=True,
        )
        measured = batcher.flush()
        self.assertEqual(measured['quantity_q'], 2.0)
        self.assertEqual(measured['non_rpi_quantity_nq'], 1.25)
        self.assertEqual(measured['q_minus_nq'], 0.75)
        self.assertEqual(measured['nq_coverage'], 1.0)
        self.assertFalse(measured['rpi_flow_research_authority'])

        batcher.push(
            receive_time_ms=3_101, event_time_ms=3_100, trade_id=31,
            price=100, qty=2, aggressive_buy=False,
        )
        unknown = batcher.flush()
        self.assertIsNone(unknown['non_rpi_quantity_nq'])
        self.assertIsNone(unknown['q_minus_nq'])
        self.assertEqual(unknown['nq_coverage'], 0.0)

    def test_cash_batch_uses_receive_time_for_freshness(self):
        rows = []

        class Store:
            @staticmethod
            def publish(record):
                rows.append(record)
                return True

        config = RecorderConfig()
        recorder = BinanceRecorder(config, Store(), HealthState(config))
        with mock.patch.object(recorder, 'now_ms', return_value=4_105):
            recorder._emit_cash_batch(
                'binance_spot_trade_100ms', 'binance_spot', {
                    'bucket_end_ms': 4_099,
                    'first_trade_id': 40, 'last_trade_id': 40,
                    'last_event_time_ms': 4_020,
                    'buy_qty': 1.0, 'sell_qty': 0.0,
                }, None,
            )
        payload = rows[0]['payload']
        self.assertEqual(rows[0]['receive_time_ms'], 4_105)
        self.assertEqual(payload['bucket_close_ms'], 4_099)
        self.assertEqual(payload['batch_available_time_ms'], 4_105)
        self.assertEqual(payload['freshness_time_basis'], 'RECEIVE_TIME')
        self.assertEqual(
            payload['causal_order_time_basis'],
            'CORRECTED_EVENT_TIME_WITH_UNCERTAINTY',
        )
        self.assertGreaterEqual(payload['clock_uncertainty_ms'], 5.0)

    def test_spot_depth_response_is_event_conditioned_and_non_authority(self):
        rows = []
        analyzer = SpotLiquidityResponseAnalyzer(
            lambda stream, payload, event_time_ms=None: rows.append(
                (stream, payload, event_time_ms)
            )
        )

        def depth(at_ms, bid='99', bid_qty='5', ask='101', ask_qty='4'):
            analyzer.observe({
                'stream': 'binance_spot_depth5',
                'receive_time_ms': at_ms,
                'payload': {
                    'bids': [[bid, bid_qty], ['98', '2']],
                    'asks': [[ask, ask_qty], ['102', '3']],
                },
            })

        depth(5_000)
        # A static depth update cannot produce a liquidity claim by itself.
        depth(5_050, bid='99.2', ask='100.8')
        self.assertEqual(rows, [])
        analyzer.observe({
            'stream': 'binance_spot_trade_100ms',
            'receive_time_ms': 5_100,
            'payload': {
                'buy_qty': 2.0, 'sell_qty': 0.25,
                'first_trade_id': 50, 'last_trade_id': 51,
                'last_event_time_ms': 5_080,
                'corrected_event_time_ms': 5_095.0,
                'clock_uncertainty_ms': 25.0, 'clock_valid': True,
            },
        })
        depth(5_150, bid='99.3', bid_qty='5.5', ask='100.9', ask_qty='3')
        depth(5_200, bid='99.4', bid_qty='6', ask='101', ask_qty='2.5')
        depth(5_350, bid='99.5', bid_qty='6', ask='101.1', ask_qty='2')
        depth(5_600, bid='99.6', bid_qty='6', ask='101.2', ask_qty='1.5')
        self.assertEqual(len(rows), 1)
        stream, payload, _ = rows[0]
        self.assertEqual(stream, 'spot_liquidity_response')
        self.assertFalse(payload['authority'])
        self.assertFalse(payload['eligible_for_live_gate'])
        self.assertFalse(payload['static_imbalance_authority'])
        self.assertEqual(
            set(payload['responses']), {'50', '100', '250', '500'}
        )
        self.assertEqual(
            payload['freshness_time_basis'], 'RECEIVE_TIME'
        )
        self.assertGreater(
            payload['responses']['500']['signed_microprice_response_bps'],
            payload['responses']['50']['signed_microprice_response_bps'],
        )
        self.assertEqual(payload['flow_price_causal_order'], 'COINCIDENT')
        self.assertEqual(payload['pre_impulse_lookback_ms'], 100)
        self.assertFalse(payload['authority'])

    def test_spot_response_marks_flow_chasing_preexisting_price_move(self):
        rows = []
        analyzer = SpotLiquidityResponseAnalyzer(
            lambda stream, payload, event_time_ms=None: rows.append(payload)
        )
        analyzer.observe({
            'stream': 'binance_spot_depth5', 'receive_time_ms': 5_000,
            'payload': {'bids': [['99', '2']], 'asks': [['101', '2']]},
        })
        analyzer.observe({
            'stream': 'binance_spot_depth5', 'receive_time_ms': 5_400,
            'payload': {'bids': [['100', '2']], 'asks': [['102', '2']]},
        })
        analyzer.observe({
            'stream': 'binance_spot_trade_100ms', 'receive_time_ms': 5_450,
            'payload': {
                'buy_qty': 2, 'sell_qty': 0, 'first_trade_id': 1,
                'last_trade_id': 1, 'last_event_time_ms': 5_440,
            },
        })
        for at_ms in (5_500, 5_550, 5_700, 5_950):
            analyzer.observe({
                'stream': 'binance_spot_depth5', 'receive_time_ms': at_ms,
                'payload': {'bids': [['100', '2']], 'asks': [['102', '2']]},
            })
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['flow_price_causal_order'], 'FLOW_CHASES_PRICE')
        self.assertGreater(rows[0]['pre_impulse_signed_mid_move_bps'], 0.0)
        self.assertFalse(rows[0]['eligible_for_live_gate'])

    def test_spot_response_waits_for_depth_and_emits_tracker_once(self):
        rows = []
        analyzer = SpotLiquidityResponseAnalyzer(
            lambda stream, payload, event_time_ms=None: rows.append(payload)
        )
        analyzer.observe({
            'stream': 'binance_spot_depth5', 'receive_time_ms': 10_000,
            'payload': {'bids': [['99', '2']], 'asks': [['101', '2']]},
        })
        analyzer.observe({
            'stream': 'binance_spot_trade_100ms', 'receive_time_ms': 10_100,
            'payload': {
                'buy_qty': 2, 'sell_qty': 0, 'first_trade_id': 1,
                'last_trade_id': 1, 'last_event_time_ms': 10_090,
            },
        })
        # Unrelated research/market rows must neither age nor finalize Spot L2.
        analyzer.observe({
            'stream': 'liquidity_response', 'receive_time_ms': 10_700,
            'payload': {'authority': False},
        })
        analyzer.observe({
            'stream': 'mark_price', 'receive_time_ms': 10_800,
            'payload': {},
        })
        self.assertEqual(rows, [])
        analyzer.observe({
            'stream': 'binance_spot_depth5', 'receive_time_ms': 10_800,
            'payload': {'bids': [['100', '2']], 'asks': [['102', '1']]},
        })
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['valid'])
        self.assertEqual(rows[0]['reason'], 'COMPLETE')
        self.assertTrue(all(rows[0]['responses'].values()))
        analyzer.observe({
            'stream': 'binance_spot_depth5', 'receive_time_ms': 10_900,
            'payload': {'bids': [['100', '2']], 'asks': [['102', '1']]},
        })
        self.assertEqual(len(rows), 1)

    def test_coinbase_timestamp_falls_back_without_regression(self):
        self.assertEqual(
            coinbase_time_ms('1970-01-01T00:00:01.250Z', 9_999), 1_250
        )
        self.assertEqual(coinbase_time_ms('invalid', 9_999), 9_999)

    def test_coinbase_subscription_snapshot_is_not_live_flow(self):
        self.assertTrue(is_coinbase_live_match('match'))
        self.assertFalse(is_coinbase_live_match('last_match'))
        self.assertFalse(is_coinbase_live_match('ticker'))


class FeatureTests(unittest.TestCase):
    def test_batched_futures_and_cash_flow_enter_one_second_feature(self):
        config = replace(RecorderConfig(), feature_lateness_seconds=1)
        health = HealthState(config)
        output = []
        engine = FeatureEngine(config, output.append, health, 'code', 'config')

        def row(stream, payload, event_ms=1100):
            return {
                'stream': stream, 'event_time_ms': event_ms,
                'receive_time_ms': event_ms + 5, 'payload': payload,
            }

        engine.process(row('futures_trade_100ms', {
            'trade_count': 3, 'buy_qty': 2, 'sell_qty': 1,
            'buy_quote': 200, 'sell_quote': 101,
            'first_price': 100, 'last_price': 101, 'high': 101, 'low': 100,
        }))
        engine.process(row('binance_spot_trade_100ms', {
            'trade_count': 2, 'buy_qty': 4, 'sell_qty': 1,
            'buy_quote': 400, 'sell_quote': 101,
            'first_price': 100, 'last_price': 101, 'high': 101, 'low': 100,
        }, 1200))
        engine.process(row('coinbase_spot_trade_100ms', {
            'trade_count': 1, 'buy_qty': 0, 'sell_qty': 3,
            'buy_quote': 0, 'sell_quote': 303,
            'first_price': 101, 'last_price': 101, 'high': 101, 'low': 101,
        }, 1300))
        engine.process(row('binance_spot_ticker', {'bid': '100', 'ask': '101'}, 1400))
        engine.process(row('mark_price', {'p': '102'}, 3100))
        payload = next(x for x in output if x['sequence_end'] == 1)['payload']
        self.assertEqual(payload['agg_trade_count'], 3)
        self.assertEqual(payload['trade_delta_qty'], 1.0)
        self.assertEqual(payload['cash_flow']['binance_spot']['buy_qty'], 4.0)
        self.assertEqual(payload['cash_flow']['coinbase_spot']['sell_qty'], 3.0)
        self.assertEqual(payload['cash_bbo']['binance_spot']['bid'], 100.0)

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
    def test_decision_tap_counts_malformed_complete_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = root / 'events.jsonl'
            events.write_bytes(b'{malformed}\n')
            config = replace(
                RecorderConfig(), data_root=root,
                journal_events_path=events,
                journal_cycles_path=root / 'cycles.json',
            )
            health = HealthState(config)
            tap = DecisionTap(config, mock.Mock(), health)
            rows, _ = tap._read_new_events()
            self.assertEqual(rows, [])
            self.assertEqual(health.decision_tap_parse_errors, 1)
            snapshot = health.snapshot()
            self.assertEqual(snapshot['decision_tap_parse_errors'], 1)
            self.assertEqual(snapshot['current_status'], 'DEGRADED')

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
            dict(base, stream='futures_trade_100ms', event_time_ms=1250,
                 receive_time_ms=1251, sequence_start=20, sequence_end=21,
                 previous_sequence=19, payload={'buy_qty': 3, 'sell_qty': 1}),
            dict(base, stream='liquidation', event_time_ms=1300,
                 receive_time_ms=1301, payload={
                     'o': {'S': 'SELL', 'z': '0.5', 'ap': '100'},
                 }),
            dict(base, source='binance_spot',
                 stream='binance_spot_trade_100ms', event_time_ms=1350,
                 receive_time_ms=1351, payload={
                     'buy_qty': 3.0, 'sell_qty': 1.0,
                 }),
            dict(base, source='coinbase_spot',
                 stream='coinbase_spot_trade_100ms', event_time_ms=1360,
                 receive_time_ms=1361, payload={
                     'buy_qty': 2.0, 'sell_qty': 4.0,
                 }),
            dict(base, stream='bot_event', event_time_ms=1400,
                 receive_time_ms=1401, payload={
                     'event': 'DECISION_EVALUATED',
                     'payload': {'setup_id': 's1', 'result': 'CORE_REJECT'},
                 }),
            dict(base, stream='feature_1s', event_time_ms=1999,
                 receive_time_ms=2000, sequence_end=1, payload={
                     'buy_qty': 5.0, 'sell_qty': 1.0,
                     'cash_flow': {
                         'binance_spot': {'buy_qty': 3.0, 'sell_qty': 1.0},
                         'coinbase_spot': {'buy_qty': 2.0, 'sell_qty': 4.0},
                     },
                 }),
        ]
        return rows

    def test_replay_is_repeatable_and_rebuilds_depth(self):
        first = DeterministicReplay(setup_id='s1').run(self.records())
        second = DeterministicReplay(setup_id='s1').run(self.records())
        self.assertEqual(first, second)
        self.assertEqual(first['depth_gaps'], 0)
        self.assertEqual(first['depth_last_u'], 12)
        self.assertEqual(first['buy_qty'], 5.0)
        self.assertEqual(first['sell_qty'], 1.0)
        self.assertEqual(first['long_liquidation_quote'], 50.0)
        self.assertEqual(first['cash_flow']['binance_spot']['buy_qty'], 3.0)
        self.assertEqual(first['cash_flow']['binance_spot']['sell_qty'], 1.0)
        self.assertEqual(first['cash_flow']['coinbase_spot']['buy_qty'], 2.0)
        self.assertEqual(first['cash_flow']['coinbase_spot']['sell_qty'], 4.0)
        self.assertEqual(first['decision_results']['CORE_REJECT'], 1)
        self.assertEqual(len(first['timeline']), 1)
        self.assertEqual(first['feature_cash_rows'], 1)
        self.assertEqual(first['feature_flow_rows'], 1)
        self.assertEqual(first['feature_flow_mismatches'], 0)
        self.assertEqual(first['sequence_gap_total'], 0)
        mirror = first['canonical_mirror']
        self.assertEqual(mirror['profile'], 'CANONICAL_MIRROR')
        self.assertEqual(mirror['contract']['bucket_ms'], 100)
        self.assertEqual(mirror['contract']['follow_max_ms'], 600)
        self.assertEqual(mirror['contract']['min_qty'], {
            'binance_spot': 0.015, 'coinbase_spot': 0.002,
            'futures': 0.15,
        })

    def test_canonical_mirror_ablation_changes_at_most_one_rule(self):
        replay = DeterministicReplay(
            wavefront=False,
            canonical_ablation={'follow_max_ms': 700},
        )
        report = replay.run([])['canonical_mirror']
        self.assertEqual(report['contract']['follow_max_ms'], 700)
        self.assertEqual(report['ablation'], {'follow_max_ms': 700})
        with self.assertRaisesRegex(ValueError, 'MUST_CHANGE_ONE_RULE'):
            DeterministicReplay(
                canonical_ablation={
                    'follow_max_ms': 700,
                    'material_price_bps': 0.20,
                }
            )

    def test_replay_detects_sequence_start_jump_even_if_previous_is_forged(self):
        rows = self.records()
        rows.insert(4, dict(
            rows[3], event_time_ms=1260, receive_time_ms=1261,
            sequence_start=25, sequence_end=25, previous_sequence=21,
        ))
        result = DeterministicReplay().run(rows)
        self.assertEqual(result['sequence_gaps']['futures_trade_100ms'], 1)

    def test_replay_reads_current_flat_decision_and_counterfactual_schema(self):
        base = self.records()[0]
        rows = [
            dict(base, stream='bot_event', event_time_ms=3000,
                 receive_time_ms=3001, payload={
                     'event': 'DECISION_EVALUATED',
                     'decision_record': {
                         'output': {
                             'decision': 'WAIT',
                             'miss_taxonomy': 'WAIT_CHASE',
                         },
                     },
                 }),
            dict(base, stream='decision_counterfactual', event_time_ms=8000,
                 receive_time_ms=8001, payload={
                     'valid': True, 'miss_taxonomy': 'WAIT_CHASE',
                     'window_seconds': 5,
                 }),
        ]
        result = DeterministicReplay().run(rows)
        self.assertEqual(result['decision_results']['WAIT'], 1)
        self.assertEqual(result['miss_taxonomy']['WAIT_CHASE'], 1)
        self.assertEqual(result['counterfactual_windows']['WAIT_CHASE:5s'], 1)

    def test_health_exposes_trade_sequence_gap(self):
        health = HealthState(RecorderConfig())
        health.sequence_gaps['futures_trade_100ms'] += 1
        snapshot = health.snapshot()
        self.assertEqual(snapshot['sequence_gap_total'], 1)
        self.assertEqual(snapshot['current_status'], 'DEGRADED')


class StorageTests(unittest.TestCase):
    def test_compactor_honors_shutdown_before_arrow_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wal = root / 'raw/wal/agg_trade/2026-08-10/08.jsonl'
            wal.parent.mkdir(parents=True)
            record = {
                'schema_version': 5, 'code_version': 'c', 'config_version': 'x',
                'source': 'test', 'symbol': 'BTCUSDT', 'stream': 'agg_trade',
                'event_time_ms': 1, 'receive_time_ms': 1,
                'sequence_start': 1, 'sequence_end': 1,
                'previous_sequence': None, 'payload': {'p': '100'},
            }
            wal.write_bytes(orjson.dumps(record) + b'\n')
            with self.assertRaises(CompactionCpuDeferred):
                compact_wal(wal, root, cpu_guard=lambda: False)
            self.assertFalse(
                (root / 'raw/parquet/agg_trade/2026-08-10/08.parquet').exists()
            )

    def test_replay_merges_parquet_and_wal_without_duplicate_hour(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = {
                'schema_version': 2, 'code_version': 'c', 'config_version': 'x',
                'source': 'test', 'symbol': 'BTCUSDT', 'stream': 'agg_trade',
                'sequence_start': 1, 'sequence_end': 1,
                'previous_sequence': None, 'payload': {'p': '100'},
            }
            old = dict(base, event_time_ms=1_000, receive_time_ms=1_001)
            old_wal = wal_path(root, old)
            old_wal.parent.mkdir(parents=True, exist_ok=True)
            old_wal.write_bytes(orjson.dumps(old) + b'\n')
            parquet = compact_wal(old_wal, root)
            self.assertTrue(parquet.exists())
            # Simulate the atomic-publish window where compacted WAL has not
            # yet been unlinked. Replay must prefer Parquet, not double count.
            newer = dict(
                base, event_time_ms=3_601_000, receive_time_ms=3_601_001,
                sequence_start=2, sequence_end=2, previous_sequence=1,
                payload={'p': '101'},
            )
            new_wal = wal_path(root, newer)
            new_wal.parent.mkdir(parents=True, exist_ok=True)
            new_wal.write_bytes(orjson.dumps(newer) + b'\n')
            rows = list(iter_merged_records(root, streams=['agg_trade']))
            self.assertEqual([row['receive_time_ms'] for row in rows], [1_001, 3_601_001])
            first = DeterministicReplay(wavefront=False).run(iter(rows))
            second = DeterministicReplay(wavefront=False).run(iter(rows))
            self.assertEqual(first['digest_sha256'], second['digest_sha256'])

    def test_parquet_compaction_requires_fresh_low_cpu_and_half_budgets(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'cpu.json'
            payload = {
                'updated_at_ms': 100_000, 'host_cpu_15m_pct': 11.0,
                'host_cpu_1h_pct': 11.0,
                'cpu_budget_15m_remaining': 181.0,
                'cpu_budget_1h_remaining': 721.0,
            }
            path.write_text(json.dumps(payload))
            self.assertTrue(cpu_allows_compaction(path, now=100.0))
            payload['host_cpu_15m_pct'] = 12.0
            path.write_text(json.dumps(payload))
            self.assertFalse(cpu_allows_compaction(path, now=100.0))
            payload['host_cpu_15m_pct'] = 11.0
            path.write_text(json.dumps(payload))
            self.assertFalse(cpu_allows_compaction(path, now=131.0))

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
            with mock.patch(
                'recorder.storage.cpu_allows_compaction', return_value=True
            ):
                self.assertTrue(store._compaction_allowed())
                store.request_shutdown()
                self.assertFalse(store._compaction_allowed())
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
