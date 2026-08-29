"""NON-AUTHORITY deterministic receive-order evidence replay.

The replay clock advances only from recorded timestamps. No market decision may
read wall-clock time through this module, which keeps repeated runs identical.
This transport/reconstruction layer does not itself reproduce or authorize the
canonical live strategy. A promotion replay needs an explicit canonical adapter.
"""

import argparse
import hashlib
import heapq
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import orjson
import pyarrow.parquet as pq

from recorder.depth import DepthGap, LocalOrderBook
from recorder.wavefront import WavefrontShadowEvaluator
from recorder.liquidity_response import LiquidityResponseAnalyzer


DEFAULT_DATA_ROOT = Path('/home/ubuntu/smc2026_data')
DEFAULT_STREAMS = {
    'depth_snapshot', 'depth_checkpoint', 'depth_diff', 'book_ticker',
    'agg_trade', 'futures_trade_100ms', 'liquidation',
    'kline_1m', 'kline_15m', 'mark_price',
    'premium_index', 'open_interest', 'bot_event', 'bot_cycles_snapshot',
    'decision_counterfactual',
    'feature_1s', 'recorder_health_event',
    'binance_spot_trade_100ms', 'binance_spot_ticker',
    'coinbase_spot_trade_100ms', 'coinbase_spot_ticker',
    'wavefront_candidate', 'wavefront_virtual_entry',
    'wavefront_virtual_exit', 'residual_edge_report', 'liquidity_response',
    'precursor_continuity',
}


def parse_time(value):
    if value is None:
        return None
    text = str(value).strip()
    try:
        number = float(text)
        return int(number if number > 10_000_000_000 else number * 1000.0)
    except ValueError:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1000)


class ReplayClock:
    def __init__(self):
        self.now_ms = 0

    @property
    def time(self):
        return self.now_ms / 1000.0

    def advance(self, receive_time_ms):
        receive_time_ms = int(receive_time_ms or 0)
        if receive_time_ms < self.now_ms:
            raise ValueError(
                f'REPLAY_CLOCK_REGRESSION current={self.now_ms} next={receive_time_ms}'
            )
        self.now_ms = receive_time_ms


def _iter_jsonl(path, start_ms=None, end_ms=None):
    with open(path, 'rb') as handle:
        for line in handle:
            if not line.strip():
                continue
            record = orjson.loads(line)
            receive_ms = int(record.get('receive_time_ms', 0) or 0)
            if start_ms is not None and receive_ms < start_ms:
                continue
            if end_ms is not None and receive_ms > end_ms:
                continue
            yield record


def wal_files(data_root, streams=None):
    root = Path(data_root) / 'raw' / 'wal'
    selected = set(streams or DEFAULT_STREAMS)
    files = []
    for stream in sorted(selected):
        files.extend(sorted((root / stream).glob('*/*.jsonl')))
    return files


def _iter_parquet(path, start_ms=None, end_ms=None):
    """Yield recorder rows from one compacted partition without full-table RAM."""
    parquet = pq.ParquetFile(path)
    columns = (
        'schema_version', 'code_version', 'config_version', 'source', 'symbol',
        'stream', 'event_time_ms', 'receive_time_ms', 'sequence_start',
        'sequence_end', 'previous_sequence', 'payload_json',
    )
    for batch in parquet.iter_batches(batch_size=2_000, columns=columns):
        for row in batch.to_pylist():
            receive_ms = int(row.get('receive_time_ms', 0) or 0)
            if start_ms is not None and receive_ms < start_ms:
                continue
            if end_ms is not None and receive_ms > end_ms:
                continue
            payload = row.pop('payload_json', b'{}') or b'{}'
            row['payload'] = orjson.loads(payload)
            yield row


def raw_files(data_root, streams=None):
    """Prefer an atomically completed Parquet hour over its remaining WAL.

    The compactor publishes Parquet with ``os.replace`` before deleting WAL.
    Choosing Parquet for an overlapping hour therefore removes duplicates while
    still covering open/uncompacted hours from WAL.
    """
    data_root = Path(data_root)
    selected = set(streams or DEFAULT_STREAMS)
    parquet_root = data_root / 'raw' / 'parquet'
    wal_root = data_root / 'raw' / 'wal'
    files = []
    compacted = set()
    for stream in sorted(selected):
        for path in sorted((parquet_root / stream).glob('*/*.parquet')):
            relative = path.relative_to(parquet_root)
            key = (relative.parts[0], relative.parts[1], Path(relative.parts[2]).stem)
            compacted.add(key)
            files.append(('parquet', path))
        for path in sorted((wal_root / stream).glob('*/*.jsonl')):
            relative = path.relative_to(wal_root)
            key = (relative.parts[0], relative.parts[1], Path(relative.parts[2]).stem)
            if key not in compacted:
                files.append(('wal', path))
    return files


def iter_merged_records(data_root, streams=None, start_ms=None, end_ms=None):
    """K-way merge Parquet plus WAL in deterministic receive order."""
    iterators = []
    heap = []
    for index, (kind, path) in enumerate(raw_files(data_root, streams)):
        source = _iter_parquet if kind == 'parquet' else _iter_jsonl
        iterator = iter(source(path, start_ms, end_ms))
        iterators.append(iterator)
        try:
            record = next(iterator)
        except StopIteration:
            continue
        key = (
            int(record.get('receive_time_ms', 0) or 0),
            int(record.get('event_time_ms', 0) or 0),
            str(record.get('stream', '')),
            index,
        )
        heapq.heappush(heap, (key, index, record))
    while heap:
        _, index, record = heapq.heappop(heap)
        yield record
        try:
            following = next(iterators[index])
        except StopIteration:
            continue
        key = (
            int(following.get('receive_time_ms', 0) or 0),
            int(following.get('event_time_ms', 0) or 0),
            str(following.get('stream', '')),
            index,
        )
        heapq.heappush(heap, (key, index, following))


class DeterministicReplay:
    def __init__(self, metrics_start_ms=None, setup_id=None, handlers=None,
                 wavefront=True, canonical_mirror=True,
                 canonical_ablation=None):
        self.clock = ReplayClock()
        self.book = LocalOrderBook()
        self.metrics_start_ms = metrics_start_ms
        self.setup_id = setup_id
        self.handlers = tuple(handlers or ())
        self.streams = Counter()
        self.decision_results = Counter()
        self.miss_taxonomy = Counter()
        self.counterfactual_windows = Counter()
        self.counterfactual_invalid = Counter()
        self.bot_events = Counter()
        self.records = 0
        self.depth_gaps = 0
        self.depth_synced = False
        self.buy_qty = 0.0
        self.sell_qty = 0.0
        self.long_liquidation_quote = 0.0
        self.short_liquidation_quote = 0.0
        self.cash_flow = {
            'binance_spot': {'buy_qty': 0.0, 'sell_qty': 0.0},
            'coinbase_spot': {'buy_qty': 0.0, 'sell_qty': 0.0},
        }
        self.sequence_gaps = Counter()
        self.last_sequence = {}
        self.feature_cash_rows = 0
        self.feature_flow_rows = 0
        self.feature_flow_mismatches = 0
        self.expected_flow = defaultdict(lambda: {
            'futures_buy': 0.0, 'futures_sell': 0.0,
            'binance_spot_buy': 0.0, 'binance_spot_sell': 0.0,
            'coinbase_spot_buy': 0.0, 'coinbase_spot_sell': 0.0,
        })
        self.timeline = []
        self.digest = hashlib.sha256()
        self.wavefront_records = []
        self.wavefront = (
            WavefrontShadowEvaluator(
                self._emit_wavefront, runtime_health_path=None,
                cpu_status_path=None,
            ) if wavefront else None
        )
        self.canonical_mirror_records = []
        self.canonical_mirror = (
            WavefrontShadowEvaluator(
                self._emit_canonical_mirror,
                runtime_health_path=None, cpu_status_path=None,
                profile="CANONICAL_MIRROR", ablation=canonical_ablation,
            ) if canonical_mirror else None
        )
        self.liquidity_response = LiquidityResponseAnalyzer(self._emit_wavefront)

    def _emit_wavefront(self, stream, payload, event_time_ms=None):
        self.wavefront_records.append({
            'stream': stream,
            'event_time_ms': int(event_time_ms or self.clock.now_ms),
            'receive_time_ms': int(self.clock.now_ms),
            'payload': payload,
        })

    def _emit_canonical_mirror(self, stream, payload, event_time_ms=None):
        self.canonical_mirror_records.append({
            'stream': stream,
            'event_time_ms': int(event_time_ms or self.clock.now_ms),
            'receive_time_ms': int(self.clock.now_ms),
            'payload': payload,
        })

    @staticmethod
    def _f(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _in_metrics(self, record):
        return (
            self.metrics_start_ms is None
            or int(record.get('receive_time_ms', 0) or 0) >= self.metrics_start_ms
        )

    def _feature_in_metrics(self, record):
        if not self._in_metrics(record):
            return False
        if self.metrics_start_ms is None:
            return True
        # Features are intentionally finalized several seconds late. Their
        # receive time may fall inside the requested range even when the actual
        # one-second bucket belongs entirely to replay warmup. Comparing such a
        # row against receive-time-filtered raw events creates a false mismatch.
        second = int(record.get('sequence_end', 0) or 0)
        return second > 0 and (second + 1) * 1000 > self.metrics_start_ms

    @staticmethod
    def _close_enough(left, right):
        scale = max(1.0, abs(float(left)), abs(float(right)))
        return abs(float(left) - float(right)) <= scale * 1e-9

    def _expected(self, record):
        return self.expected_flow[int(record.get('event_time_ms', 0) or 0) // 1000]

    def apply(self, record):
        self.clock.advance(record.get('receive_time_ms', 0))
        stream = str(record.get('stream', ''))
        payload = record.get('payload', {}) or {}
        in_metrics = self._in_metrics(record)
        if in_metrics:
            self.records += 1
            self.streams[stream] += 1
            self.digest.update(orjson.dumps(record, option=orjson.OPT_SORT_KEYS))

        if stream in (
            'futures_trade_100ms', 'binance_spot_trade_100ms',
            'coinbase_spot_trade_100ms',
        ):
            previous = record.get('previous_sequence')
            sequence_start = record.get('sequence_start')
            last = self.last_sequence.get(stream)
            discontinuity = bool(
                last is not None and (
                    (previous is not None and int(previous) != last)
                    or (
                        sequence_start is not None
                        and int(sequence_start) != last + 1
                    )
                )
            )
            if discontinuity:
                if in_metrics:
                    self.sequence_gaps[stream] += 1
            sequence_end = record.get('sequence_end')
            if sequence_end is not None:
                self.last_sequence[stream] = int(sequence_end)

        if stream == 'depth_snapshot':
            self.book.reset(payload)
            self.depth_synced = False
        elif stream == 'depth_checkpoint':
            self.book.reset_checkpoint(payload)
            self.depth_synced = True
        elif stream == 'depth_diff' and (
            payload.get('partial') or self.book.snapshot_update_id is not None
        ):
            try:
                status = (
                    self.book.apply_partial(payload)
                    if payload.get('partial') else self.book.apply(payload)
                )
                self.depth_synced = status == 'APPLIED' or self.book.synced
            except DepthGap:
                if in_metrics:
                    self.depth_gaps += 1
                self.depth_synced = False
        elif stream == 'agg_trade' and in_metrics:
            qty = self._f(payload.get('q'))
            if bool(payload.get('m')):
                self.sell_qty += qty
                self._expected(record)['futures_sell'] += qty
            else:
                self.buy_qty += qty
                self._expected(record)['futures_buy'] += qty
        elif stream == 'futures_trade_100ms' and in_metrics:
            buy = self._f(payload.get('buy_qty'))
            sell = self._f(payload.get('sell_qty'))
            self.buy_qty += buy
            self.sell_qty += sell
            expected = self._expected(record)
            expected['futures_buy'] += buy
            expected['futures_sell'] += sell
        elif stream == 'liquidation' and in_metrics:
            order = payload.get('o', payload) or {}
            qty = self._f(order.get('z') or order.get('q'))
            price = self._f(order.get('ap') or order.get('p'))
            if str(order.get('S', '')).upper() == 'SELL':
                self.long_liquidation_quote += qty * price
            elif str(order.get('S', '')).upper() == 'BUY':
                self.short_liquidation_quote += qty * price
        elif stream in (
            'binance_spot_trade_100ms', 'coinbase_spot_trade_100ms'
        ) and in_metrics:
            venue = (
                'binance_spot'
                if stream.startswith('binance_') else 'coinbase_spot'
            )
            self.cash_flow[venue]['buy_qty'] += self._f(payload.get('buy_qty'))
            self.cash_flow[venue]['sell_qty'] += self._f(payload.get('sell_qty'))
            expected = self._expected(record)
            expected[f'{venue}_buy'] += self._f(payload.get('buy_qty'))
            expected[f'{venue}_sell'] += self._f(payload.get('sell_qty'))
        elif stream == 'bot_event' and in_metrics:
            event = str(payload.get('event', 'UNKNOWN'))
            # Current WStrade journal events are flat. Historical SMC rows used
            # a nested `payload`; support both without silently returning UNKNOWN.
            event_payload = payload.get('payload') or payload
            self.bot_events[event] += 1
            if event == 'DECISION_EVALUATED':
                decision_record = event_payload.get('decision_record') or {}
                output = decision_record.get('output') or {}
                decision = (
                    output.get('decision') or event_payload.get('decision')
                    or event_payload.get('result') or 'UNKNOWN'
                )
                miss = output.get('miss_taxonomy') or event_payload.get('miss_taxonomy')
                self.decision_results[str(decision)] += 1
                if miss:
                    self.miss_taxonomy[str(miss)] += 1
            if self.setup_id and event_payload.get('setup_id') == self.setup_id:
                self.timeline.append(payload)
        elif stream == 'decision_counterfactual' and in_metrics:
            if payload.get('valid'):
                key = '%s:%ss' % (
                    payload.get('miss_taxonomy', 'UNKNOWN'),
                    int(payload.get('window_seconds', 0) or 0),
                )
                self.counterfactual_windows[key] += 1
            else:
                self.counterfactual_invalid[str(
                    payload.get('invalid_reason', 'UNKNOWN')
                )] += 1
        elif stream == 'feature_1s' and self._feature_in_metrics(record):
            cash = payload.get('cash_flow', {}) or {}
            if all(venue in cash for venue in ('binance_spot', 'coinbase_spot')):
                self.feature_cash_rows += 1
                second = int(record.get('sequence_end', 0) or 0)
                expected = self.expected_flow.get(second)
                if expected is not None:
                    self.feature_flow_rows += 1
                    observed = {
                        'futures_buy': self._f(payload.get('buy_qty')),
                        'futures_sell': self._f(payload.get('sell_qty')),
                        'binance_spot_buy': self._f(
                            (cash.get('binance_spot') or {}).get('buy_qty')
                        ),
                        'binance_spot_sell': self._f(
                            (cash.get('binance_spot') or {}).get('sell_qty')
                        ),
                        'coinbase_spot_buy': self._f(
                            (cash.get('coinbase_spot') or {}).get('buy_qty')
                        ),
                        'coinbase_spot_sell': self._f(
                            (cash.get('coinbase_spot') or {}).get('sell_qty')
                        ),
                    }
                    if any(
                        not self._close_enough(observed[key], expected[key])
                        for key in expected
                    ):
                        self.feature_flow_mismatches += 1

        for handler in self.handlers:
            handler(record, self.clock)
        if self.wavefront is not None:
            self.wavefront.observe(record)
        if self.canonical_mirror is not None:
            self.canonical_mirror.observe(record)
        self.liquidity_response.observe(record)

    def run(self, records):
        for record in records:
            self.apply(record)
        return self.summary()

    def summary(self):
        return {
            'records': self.records,
            'clock_end_ms': self.clock.now_ms,
            'streams': dict(self.streams),
            'depth_synced': self.depth_synced,
            'depth_last_u': self.book.last_u,
            'depth_gaps': self.depth_gaps,
            'sequence_gaps': dict(self.sequence_gaps),
            'sequence_gap_total': sum(self.sequence_gaps.values()),
            'feature_cash_rows': self.feature_cash_rows,
            'feature_flow_rows': self.feature_flow_rows,
            'feature_flow_mismatches': self.feature_flow_mismatches,
            'buy_qty': self.buy_qty,
            'sell_qty': self.sell_qty,
            'trade_delta_qty': self.buy_qty - self.sell_qty,
            'long_liquidation_quote': self.long_liquidation_quote,
            'short_liquidation_quote': self.short_liquidation_quote,
            'cash_flow': {
                venue: dict(values) for venue, values in self.cash_flow.items()
            },
            'bot_events': dict(self.bot_events),
            'decision_results': dict(self.decision_results),
            'miss_taxonomy': dict(self.miss_taxonomy),
            'counterfactual_windows': dict(self.counterfactual_windows),
            'counterfactual_invalid': dict(self.counterfactual_invalid),
            'digest_sha256': self.digest.hexdigest(),
            'timeline': self.timeline,
            'wavefront': self.wavefront.summary() if self.wavefront else None,
            'wavefront_generated_records': len(self.wavefront_records),
            'canonical_mirror': (
                self.canonical_mirror.summary()
                if self.canonical_mirror else None
            ),
            'canonical_mirror_generated_records': len(
                self.canonical_mirror_records
            ),
            'liquidity_response': self.liquidity_response.summary(),
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Deterministic SMC2026 WAL replay')
    parser.add_argument('--data-root', default=str(DEFAULT_DATA_ROOT))
    parser.add_argument('--start', help='epoch seconds/ms or ISO-8601')
    parser.add_argument('--end', help='epoch seconds/ms or ISO-8601')
    parser.add_argument('--warmup-seconds', type=int, default=90)
    parser.add_argument('--stream', action='append', dest='streams')
    parser.add_argument('--setup-id')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    from loi_he_thong.runtime_lock import DuplicateInstanceError, acquire_runtime_lock
    try:
        lock = acquire_runtime_lock('bot')
    except DuplicateInstanceError as exc:
        parser.error(f'replay cannot overlap the production bot: {exc}')
    try:
        start_ms = parse_time(args.start)
        end_ms = parse_time(args.end)
        read_start = (
            start_ms - max(0, args.warmup_seconds) * 1000
            if start_ms is not None else None
        )
        records = iter_merged_records(
            args.data_root, streams=args.streams,
            start_ms=read_start, end_ms=end_ms,
        )
        result = DeterministicReplay(
            metrics_start_ms=start_ms, setup_id=args.setup_id
        ).run(records)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        if args.output:
            Path(args.output).write_text(rendered + '\n')
        else:
            print(rendered)
        return 0 if result['depth_gaps'] == 0 else 2
    finally:
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
