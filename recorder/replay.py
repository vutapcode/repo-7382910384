"""Deterministic receive-order replay for the SMC2026 black box.

The replay clock advances only from recorded timestamps. No market decision may
read wall-clock time through this module, which keeps repeated runs identical.
"""

import argparse
import hashlib
import heapq
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import orjson

from recorder.depth import DepthGap, LocalOrderBook


DEFAULT_DATA_ROOT = Path('/home/ubuntu/smc2026_data')
DEFAULT_STREAMS = {
    'depth_snapshot', 'depth_checkpoint', 'depth_diff', 'book_ticker',
    'agg_trade', 'liquidation', 'kline_1m', 'kline_15m', 'mark_price',
    'premium_index', 'open_interest', 'bot_event', 'bot_cycles_snapshot',
    'feature_1s', 'recorder_health_event',
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


def iter_merged_records(data_root, streams=None, start_ms=None, end_ms=None):
    """K-way merge keeps memory bounded even across large raw partitions."""
    iterators = []
    heap = []
    for index, path in enumerate(wal_files(data_root, streams)):
        iterator = iter(_iter_jsonl(path, start_ms, end_ms))
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
    def __init__(self, metrics_start_ms=None, setup_id=None, handlers=None):
        self.clock = ReplayClock()
        self.book = LocalOrderBook()
        self.metrics_start_ms = metrics_start_ms
        self.setup_id = setup_id
        self.handlers = tuple(handlers or ())
        self.streams = Counter()
        self.decision_results = Counter()
        self.bot_events = Counter()
        self.records = 0
        self.depth_gaps = 0
        self.depth_synced = False
        self.buy_qty = 0.0
        self.sell_qty = 0.0
        self.long_liquidation_quote = 0.0
        self.short_liquidation_quote = 0.0
        self.timeline = []
        self.digest = hashlib.sha256()

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

    def apply(self, record):
        self.clock.advance(record.get('receive_time_ms', 0))
        stream = str(record.get('stream', ''))
        payload = record.get('payload', {}) or {}
        in_metrics = self._in_metrics(record)
        if in_metrics:
            self.records += 1
            self.streams[stream] += 1
            self.digest.update(orjson.dumps(record, option=orjson.OPT_SORT_KEYS))

        if stream == 'depth_snapshot':
            self.book.reset(payload)
            self.depth_synced = False
        elif stream == 'depth_checkpoint':
            self.book.reset_checkpoint(payload)
            self.depth_synced = True
        elif stream == 'depth_diff' and self.book.snapshot_update_id is not None:
            try:
                status = self.book.apply(payload)
                self.depth_synced = status == 'APPLIED' or self.book.synced
            except DepthGap:
                if in_metrics:
                    self.depth_gaps += 1
                self.depth_synced = False
        elif stream == 'agg_trade' and in_metrics:
            qty = self._f(payload.get('q'))
            if bool(payload.get('m')):
                self.sell_qty += qty
            else:
                self.buy_qty += qty
        elif stream == 'liquidation' and in_metrics:
            order = payload.get('o', payload) or {}
            qty = self._f(order.get('z') or order.get('q'))
            price = self._f(order.get('ap') or order.get('p'))
            if str(order.get('S', '')).upper() == 'SELL':
                self.long_liquidation_quote += qty * price
            elif str(order.get('S', '')).upper() == 'BUY':
                self.short_liquidation_quote += qty * price
        elif stream == 'bot_event' and in_metrics:
            event = str(payload.get('event', 'UNKNOWN'))
            event_payload = payload.get('payload', {}) or {}
            self.bot_events[event] += 1
            if event == 'DECISION_EVALUATED':
                self.decision_results[str(event_payload.get('result', 'UNKNOWN'))] += 1
            if self.setup_id and event_payload.get('setup_id') == self.setup_id:
                self.timeline.append(payload)

        for handler in self.handlers:
            handler(record, self.clock)

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
            'buy_qty': self.buy_qty,
            'sell_qty': self.sell_qty,
            'trade_delta_qty': self.buy_qty - self.sell_qty,
            'long_liquidation_quote': self.long_liquidation_quote,
            'short_liquidation_quote': self.short_liquidation_quote,
            'bot_events': dict(self.bot_events),
            'decision_results': dict(self.decision_results),
            'digest_sha256': self.digest.hexdigest(),
            'timeline': self.timeline,
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


if __name__ == '__main__':
    raise SystemExit(main())
