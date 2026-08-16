import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '4_nghien_cuu_ai'))

from gemini_shadow.config import ShadowConfig
from gemini_shadow.data import RecorderReader, build_cycle_envelope
from gemini_shadow.redact import redact
from gemini_shadow.schema import validate_analysis
from gemini_shadow.storage import ShadowStore
from gemini_shadow.worker import ShadowWorker


VALID_ANALYSIS = {
    'market_regime': 'RANGE',
    'summary': 'Flat market with weak follow-through.',
    'failure_causes': ['Fees exceeded the captured move.'],
    'data_quality_flags': [],
    'recommendations': ['Replay a stricter fee-floor hypothesis.'],
    'supporting_evidence': ['The setup had directional flow confirmation.'],
    'contradicting_evidence': ['Observed fees exceeded the captured move.'],
    'confidence': 0.8,
}


def write_record(path, stream, event_ms, payload, sequence=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        'stream': stream, 'event_time_ms': event_ms,
        'sequence_start': sequence, 'payload': payload,
    }
    with path.open('ab') as handle:
        handle.write(json.dumps(record).encode() + b'\n')


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    async def analyze(self, envelope):
        self.calls += 1
        if self.error:
            raise self.error
        return dict(VALID_ANALYSIS), {'input_tokens': 100, 'output_tokens': 40}


class StaticReader:
    def __init__(self, cycles):
        self.cycles = cycles

    def load_cycles(self):
        return list(self.cycles)

    def read_stream(self, stream, ranges):
        return [], 0


class GeminiShadowTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_rejects_extra_fields_and_invalid_confidence(self):
        valid = validate_analysis(dict(VALID_ANALYSIS))
        self.assertEqual(valid['market_regime'], 'RANGE')
        with self.assertRaises(ValueError):
            validate_analysis({**VALID_ANALYSIS, 'trade_now': True})
        with self.assertRaises(ValueError):
            validate_analysis({**VALID_ANALYSIS, 'confidence': 1.2})

    def test_redaction_removes_nested_and_inline_secrets(self):
        value = {
            'api_key': 'abc',
            'nested': {'note': 'token=xyz', 'safe': 'ok'},
            'text': 'literal-private-value',
        }
        result = redact(value, secrets=('literal-private-value',))
        self.assertEqual(result['api_key'], '[REDACTED]')
        self.assertNotIn('xyz', result['nested']['note'])
        self.assertEqual(result['text'], '[REDACTED]')

    def test_store_is_append_only_and_deduplicates_input_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ShadowStore(temp)
            record = {'input_hash': 'hash-1', 'analysis_id': 'one'}
            self.assertTrue(store.append(record))
            self.assertFalse(store.append(record))
            reloaded = ShadowStore(temp)
            self.assertTrue(reloaded.contains('hash-1'))
            files = list((Path(temp) / 'records').glob('*.jsonl'))
            self.assertEqual(len(files), 1)
            self.assertEqual(len(files[0].read_text().splitlines()), 1)

    def test_reader_skips_malformed_rows_and_preserves_cycle_linkage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cycles_path = root / 'cycles.json'
            cycle = {
                'position_cycle_id': 'pc-1', 'setup_id': 'setup-1',
                'setup_generation': 7, 'symbol': 'BTCUSDT', 'mode': 'TREND-PULLBACK',
                'bias': 'LONG', 'status': 'CLOSED', 'created_at': 1000.0,
                'closed_at': 1010.0, 'actual': {}, 'shadow': {},
            }
            cycles_path.write_text(json.dumps({'cycles': [cycle]}))
            moment_path = root / 'raw/wal/feature_1s/1970-01-01/00.jsonl'
            write_record(moment_path, 'feature_1s', 1_000_000, {
                'last_trade_price': 100.0, 'buy_qty': 2.0, 'sell_qty': 1.0,
                'trade_delta_qty': 1.0, 'event_counts': {'agg_trade': 2},
            })
            with moment_path.open('ab') as handle:
                handle.write(b'{bad json\n')
            config = replace(
                ShadowConfig(), data_root=root, cycles_path=cycles_path,
                window_seconds=30,
            )
            reader = RecorderReader(root, cycles_path)
            envelope, input_hash = build_cycle_envelope(config, reader, cycle)
            self.assertEqual(envelope['linkage']['position_cycle_id'], 'pc-1')
            self.assertEqual(envelope['linkage']['setup_id'], 'setup-1')
            self.assertEqual(len(input_hash), 64)
            self.assertIn('MALFORMED_WAL_ROWS_SKIPPED',
                          envelope['market_context']['data_quality']['flags'])

    def test_reader_deduplicates_overlapping_feature_instances_by_second(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'raw/wal/feature_1s/1970-01-01/00.jsonl'
            base = {
                'last_trade_price': 100.0,
                'buy_qty': 2.0,
                'sell_qty': 1.0,
                'trade_delta_qty': 1.0,
            }
            write_record(path, 'feature_1s', 1_000, {**base, 'cvd_btc': 5.0})
            write_record(path, 'feature_1s', 1_000, {**base, 'cvd_btc': 500.0})
            reader = RecorderReader(root, root / 'cycles.json')
            rows, malformed = reader.read_stream('feature_1s', [(0.0, 2.0)])
            self.assertEqual(malformed, 0)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['payload']['trade_delta_qty'], 1.0)

    def test_shadow_context_surfaces_macro_freshness_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cycles_path = root / 'cycles.json'
            cycle = {
                'position_cycle_id': 'pc-macro', 'setup_id': 'setup-macro',
                'status': 'CLOSED', 'created_at': 1.0, 'closed_at': 1.0,
                'actual': {}, 'shadow': {},
            }
            cycles_path.write_text(json.dumps({'cycles': [cycle]}))
            path = root / 'raw/wal/feature_1s/1970-01-01/00.jsonl'
            write_record(path, 'feature_1s', 1_000, {
                'last_trade_price': 100.0,
                'macro': {},
                'macro_complete': False,
                'macro_fresh': {'open_interest': False},
            })
            config = replace(
                ShadowConfig(), data_root=root, cycles_path=cycles_path,
                window_seconds=1,
            )
            envelope, _ = build_cycle_envelope(
                config, RecorderReader(root, cycles_path), cycle
            )
            quality = envelope['market_context']['data_quality']
            self.assertEqual(quality['macro_complete_ratio'], 0.0)
            self.assertEqual(quality['fresh_open_interest_ratio'], 0.0)
            self.assertIn('MACRO_FEATURE_COVERAGE_BELOW_80_PERCENT', quality['flags'])
            self.assertIn('OPEN_INTEREST_FRESHNESS_BELOW_80_PERCENT', quality['flags'])

    async def test_success_writes_shadow_record_without_trading_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cycle = {
                'position_cycle_id': 'pc-success', 'setup_id': 'setup-success',
                'setup_generation': 1, 'status': 'CLOSED',
                'created_at': 1000.0, 'closed_at': 1010.0,
                'actual': {}, 'shadow': {},
            }
            config = replace(
                ShadowConfig(), data_root=root, cycles_path=root / 'cycles.json',
                retries=1, window_seconds=30,
            )
            store = ShadowStore(config.output_root)
            client = FakeClient()
            worker = ShadowWorker(config, StaticReader([cycle]), store, client)
            outcomes = await worker.run_once(
                now=1020.0, replay_limit=1, include_regime=False
            )
            self.assertEqual(outcomes[0]['result'], 'WRITTEN')
            path = next((config.output_root / 'records').glob('*.jsonl'))
            record = json.loads(path.read_text())
            self.assertEqual(record['position_cycle_id'], 'pc-success')
            self.assertEqual(record['authority'], 'SHADOW_RESEARCH_ONLY')
            self.assertNotIn('action', record)
            outcomes = await worker.run_once(
                now=1020.0, replay_limit=1, include_regime=False
            )
            self.assertEqual(outcomes[0]['result'], 'DEDUPED')
            self.assertEqual(client.calls, 1)

    async def test_live_cycle_waits_for_post_close_window_to_settle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cycle = {
                'position_cycle_id': 'pc-settle', 'setup_id': 'setup-settle',
                'status': 'CLOSED', 'created_at': 1000.0, 'closed_at': 1010.0,
                'actual': {}, 'shadow': {},
            }
            config = replace(
                ShadowConfig(), data_root=root, cycles_path=root / 'cycles.json',
                retries=1, window_seconds=30, cycle_lookback_hours=1,
            )
            store = ShadowStore(config.output_root)
            client = FakeClient()
            worker = ShadowWorker(config, StaticReader([cycle]), store, client)
            early = await worker.run_once(now=1039.0, include_regime=False)
            self.assertEqual(early, [])
            ready = await worker.run_once(now=1040.0, include_regime=False)
            self.assertEqual(ready[0]['result'], 'WRITTEN')

    async def test_model_failure_only_writes_health_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cycle = {
                'position_cycle_id': 'pc-fail', 'setup_id': 'setup-fail',
                'status': 'ABORTED', 'created_at': 1000.0, 'closed_at': 1001.0,
                'actual': {}, 'shadow': {},
            }
            config = replace(
                ShadowConfig(), data_root=root, cycles_path=root / 'cycles.json',
                retries=1, window_seconds=30,
            )
            store = ShadowStore(config.output_root)
            worker = ShadowWorker(
                config, StaticReader([cycle]), store,
                FakeClient(RuntimeError('api_key=do-not-log')),
            )
            outcomes = await worker.run_once(
                now=1002.0, replay_limit=1, include_regime=False
            )
            self.assertEqual(outcomes[0]['result'], 'ERROR')
            self.assertFalse((config.output_root / 'records').exists())
            health = next((config.output_root / 'health').glob('*.jsonl')).read_text()
            self.assertNotIn('do-not-log', health)

    async def test_rate_limit_opens_persistent_cooldown_without_retry_storm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cycle = {
                'position_cycle_id': 'pc-429', 'setup_id': 'setup-429',
                'status': 'CLOSED', 'created_at': 1000.0, 'closed_at': 1001.0,
                'actual': {}, 'shadow': {},
            }
            config = replace(
                ShadowConfig(), data_root=root, cycles_path=root / 'cycles.json',
                retries=3, window_seconds=30, failure_base_seconds=60,
            )
            client = FakeClient(RuntimeError('429 RESOURCE_EXHAUSTED retryDelay: 90s'))
            store = ShadowStore(config.output_root)
            worker = ShadowWorker(config, StaticReader([cycle]), store, client)
            first = await worker.run_once(
                now=1002.0, replay_limit=1, include_regime=False
            )
            second = await worker.run_once(
                now=1002.0, replay_limit=1, include_regime=False
            )
            self.assertEqual(first[0]['result'], 'ERROR')
            self.assertEqual(second[0]['result'], 'CIRCUIT_COOLDOWN')
            self.assertEqual(client.calls, 1)
            reloaded = ShadowStore(config.output_root)
            allowed, _ = reloaded.attempt_allowed(first[0]['input_hash'])
            self.assertFalse(allowed)

    def test_worker_rejects_stale_recorder_health(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            health = root / 'health' / 'status.json'
            health.parent.mkdir(parents=True)
            health.write_text(json.dumps({
                'updated_at_ms': 1000, 'current_status': 'OK',
            }))
            config = replace(
                ShadowConfig(), data_root=root, recorder_stale_seconds=15,
            )
            worker = ShadowWorker(config, StaticReader([]), ShadowStore(config.output_root))
            fresh, reason = worker._recorder_fresh(now=20.0)
            self.assertFalse(fresh)
            self.assertEqual(reason, 'RECORDER_HEALTH_STALE')

    def test_shadow_package_has_no_live_trading_imports(self):
        package = Path(__file__).resolve().parents[1] / '4_nghien_cuu_ai/gemini_shadow'
        forbidden = (
            'dat_lenh', 'chi_huy_truong', 'binance_api', 'SharedState',
            'loi_he_thong',
        )
        source = '\n'.join(
            path.read_text(encoding='utf-8')
            for path in package.glob('*.py')
        )
        for marker in forbidden:
            self.assertNotIn(marker, source)


if __name__ == '__main__':
    unittest.main()
