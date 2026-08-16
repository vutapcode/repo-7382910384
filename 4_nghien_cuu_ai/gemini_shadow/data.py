"""Read-only recorder/journal adapter and deterministic prompt feature builder."""

import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import orjson


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def canonical_hash(value):
    payload = orjson.dumps(value, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(payload).hexdigest()


def merge_ranges(ranges):
    clean = sorted((float(start), float(end)) for start, end in ranges if end >= start)
    merged = []
    for start, end in clean:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def cycle_time_ranges(cycle, radius_seconds):
    anchors = []
    for field in ('created_at', 'closed_at'):
        value = _f(cycle.get(field))
        if value and value > 0:
            anchors.append(value)
    for order in ((cycle.get('actual') or {}).get('orders') or []):
        submitted = _f(order.get('submitted_at'))
        if submitted and submitted > 0:
            anchors.append(submitted)
        for fill in order.get('fills') or []:
            filled = _f(fill.get('time'))
            if filled and filled > 0:
                anchors.append(filled / 1000.0 if filled > 10_000_000_000 else filled)
    if not anchors:
        return []
    return merge_ranges((anchor - radius_seconds, anchor + radius_seconds) for anchor in anchors)


def cycle_ready_at(cycle, radius_seconds):
    ranges = cycle_time_ranges(cycle, radius_seconds)
    return max((end for _, end in ranges), default=float('inf'))


class RecorderReader:
    def __init__(self, data_root, cycles_path):
        self.data_root = Path(data_root)
        self.cycles_path = Path(cycles_path)

    def load_cycles(self):
        try:
            payload = orjson.loads(self.cycles_path.read_bytes())
        except (OSError, orjson.JSONDecodeError):
            return []
        cycles = payload.get('cycles', []) if isinstance(payload, dict) else []
        return [item for item in cycles if isinstance(item, dict)]

    def _paths_for_ranges(self, stream, ranges):
        base = self.data_root / 'raw' / 'wal' / stream
        paths = set()
        for start, end in ranges:
            # WAL is partitioned by receive time; one-hour padding tolerates
            # delayed events without scanning the whole archive.
            first = int(start // 3600) - 1
            last = int(end // 3600) + 1
            for hour in range(first, last + 1):
                moment = datetime.fromtimestamp(hour * 3600, tz=timezone.utc)
                path = base / moment.strftime('%Y-%m-%d') / moment.strftime('%H.jsonl')
                if path.exists():
                    paths.add(path)
        return sorted(paths)

    def read_stream(self, stream, ranges):
        ranges = merge_ranges(ranges)
        rows, malformed, seen = [], 0, set()
        for path in self._paths_for_ranges(stream, ranges):
            try:
                handle = path.open('rb')
            except OSError:
                continue
            with handle:
                for line in handle:
                    try:
                        record = orjson.loads(line)
                    except orjson.JSONDecodeError:
                        malformed += 1
                        continue
                    event_ms = int(record.get('event_time_ms', 0) or 0)
                    event_seconds = event_ms / 1000.0
                    if not any(start <= event_seconds <= end for start, end in ranges):
                        continue
                    if stream == 'feature_1s':
                        # A feature row is one logical symbol-second.  Older
                        # recorder overlaps produced duplicate rows whose only
                        # difference was process-lifetime CVD; treating their
                        # payload hash as identity double-counted market flow.
                        identity = (
                            record.get('symbol'), event_ms,
                            record.get('sequence_start'),
                            record.get('sequence_end'),
                        )
                    else:
                        identity = (
                            event_ms, record.get('sequence_start'),
                            canonical_hash(record.get('payload', {})),
                        )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    rows.append(record)
        rows.sort(key=lambda item: int(item.get('event_time_ms', 0) or 0))
        return rows, malformed


def _selected_cycle(cycle):
    actual = cycle.get('actual') or {}
    shadow = cycle.get('shadow') or {}
    economic = cycle.get('economic_observation') or {}
    return {
        'position_cycle_id': cycle.get('position_cycle_id'),
        'setup_id': cycle.get('setup_id'),
        'setup_generation': cycle.get('setup_generation'),
        'symbol': cycle.get('symbol'),
        'mode': cycle.get('mode'),
        'bias': cycle.get('bias'),
        'status': cycle.get('status'),
        'created_at': cycle.get('created_at'),
        'closed_at': cycle.get('closed_at'),
        'session': cycle.get('session'),
        'volatility': cycle.get('volatility'),
        'score': cycle.get('score'),
        'requested_qty': cycle.get('requested_qty'),
        'size_policy': cycle.get('size_policy'),
        'protection_policy': cycle.get('protection_policy'),
        'signal_price': cycle.get('signal_price'),
        'decision_price': cycle.get('decision_price'),
        'entry_reference_price': cycle.get('entry_reference_price'),
        'entry_reason': cycle.get('entry_reason'),
        'exit_reason': cycle.get('exit_reason'),
        'abort_reason': cycle.get('abort_reason'),
        'execution_purpose': cycle.get('execution_purpose'),
        'economic_result_valid': cycle.get('economic_result_valid'),
        'economic_invalid_reason': cycle.get('economic_invalid_reason'),
        'economic': {
            key: economic.get(key) for key in (
                'economic_pass', 'execution_floor_pass', 'expected_edge_pass',
                'tp1_distance_bps', 'projected_capture_bps', 'all_in_cost_bps',
                'expected_net_edge_bps', 'reason', 'structural_fee_floor_reason',
            )
        },
        'actual': {
            key: actual.get(key) for key in (
                'valid_for_strategy_evaluation', 'entry_fill_price',
                'exit_fill_price', 'gross_pnl_quote', 'fee_quote',
                'net_pnl_quote', 'gross_pnl_bps', 'fee_bps', 'net_pnl_bps',
            )
        },
        'shadow': {
            key: shadow.get(key) for key in (
                'valid_for_strategy_evaluation', 'status', 'shadow_kind',
                'entry_price', 'exit_price', 'gross_pnl_bps', 'fee_bps',
                'net_pnl_bps', 'mfe_bps', 'mae_bps', 'exit_reason',
                'sample_independence',
            )
        },
    }


def _minute_summary(records):
    buckets = defaultdict(lambda: {
        'prices': [], 'trade_qty': 0.0, 'delta_qty': 0.0,
        'spread_bps': [], 'obi_5bps': [], 'delay_ms_max': 0.0,
        'funding_rate': None, 'open_interest': None, 'events': Counter(),
    })
    for record in records:
        payload = record.get('payload') or {}
        minute = int(record.get('event_time_ms', 0) or 0) // 60_000 * 60_000
        bucket = buckets[minute]
        price = _f(payload.get('last_trade_price'))
        if price is None:
            price = _f(payload.get('mid_close'))
        if price is not None and price > 0:
            bucket['prices'].append(price)
        bucket['trade_qty'] += max(0.0, _f(payload.get('buy_qty'), 0.0) or 0.0)
        bucket['trade_qty'] += max(0.0, _f(payload.get('sell_qty'), 0.0) or 0.0)
        bucket['delta_qty'] += _f(payload.get('trade_delta_qty'), 0.0) or 0.0
        spread = _f(payload.get('spread_bps_mean'))
        if spread is not None:
            bucket['spread_bps'].append(spread)
        book = payload.get('book') or {}
        obi = _f(book.get('obi_5bps'))
        if obi is not None:
            bucket['obi_5bps'].append(obi)
        bucket['delay_ms_max'] = max(
            bucket['delay_ms_max'], _f(payload.get('delay_ms_max'), 0.0) or 0.0
        )
        macro = payload.get('macro') or {}
        if macro.get('funding_rate') is not None:
            bucket['funding_rate'] = _f(macro.get('funding_rate'))
        if macro.get('open_interest') is not None:
            bucket['open_interest'] = _f(macro.get('open_interest'))
        bucket['events'].update(payload.get('event_counts') or {})

    result = []
    for minute, bucket in sorted(buckets.items()):
        prices = bucket.pop('prices')
        spreads = bucket.pop('spread_bps')
        obis = bucket.pop('obi_5bps')
        result.append({
            'minute_ms': minute,
            'open': prices[0] if prices else None,
            'high': max(prices) if prices else None,
            'low': min(prices) if prices else None,
            'close': prices[-1] if prices else None,
            'trade_qty': round(bucket['trade_qty'], 8),
            'delta_qty': round(bucket['delta_qty'], 8),
            'spread_bps_mean': round(sum(spreads) / len(spreads), 6) if spreads else None,
            'obi_5bps_mean': round(sum(obis) / len(obis), 6) if obis else None,
            'delay_ms_max': bucket['delay_ms_max'],
            'funding_rate': bucket['funding_rate'],
            'open_interest': bucket['open_interest'],
            'event_counts': dict(bucket['events']),
        })
    return result


def _bot_summary(records):
    events, decisions = Counter(), Counter()
    setup_ids, cycle_ids = set(), set()
    for record in records:
        outer = record.get('payload') or {}
        event = str(outer.get('event') or 'UNKNOWN')
        events[event] += 1
        body = outer.get('payload') or {}
        if event == 'DECISION_EVALUATED':
            decisions[str(body.get('result') or 'UNKNOWN')] += 1
        if body.get('setup_id'):
            setup_ids.add(str(body['setup_id']))
        if outer.get('position_cycle_id'):
            cycle_ids.add(str(outer['position_cycle_id']))
    return {
        'event_counts': dict(events),
        'decision_result_counts': dict(decisions),
        'setup_ids': sorted(setup_ids)[:100],
        'position_cycle_ids': sorted(cycle_ids)[:100],
    }


def build_market_context(reader, ranges):
    ranges = merge_ranges(ranges)
    features, malformed_features = reader.read_stream('feature_1s', ranges)
    bot_events, malformed_bot = reader.read_stream('bot_event', ranges)
    seconds = {int(row.get('event_time_ms', 0) or 0) // 1000 for row in features}
    expected = max(1, sum(int(end - start) + 1 for start, end in ranges))
    coverage = min(1.0, len(seconds) / expected)
    quality_rows = [
        row.get('payload') or {}
        for row in features
        if 'macro_fresh' in (row.get('payload') or {})
    ]
    macro_complete_ratio = None
    fresh_oi_ratio = None
    if quality_rows:
        macro_complete_ratio = sum(
            bool(payload.get('macro_complete')) for payload in quality_rows
        ) / len(quality_rows)
        fresh_oi_ratio = sum(
            bool((payload.get('macro_fresh') or {}).get('open_interest'))
            for payload in quality_rows
        ) / len(quality_rows)
    flags = []
    if coverage < 0.95:
        flags.append('FEATURE_1S_COVERAGE_BELOW_95_PERCENT')
    if malformed_features or malformed_bot:
        flags.append('MALFORMED_WAL_ROWS_SKIPPED')
    if macro_complete_ratio is not None and macro_complete_ratio < 0.80:
        flags.append('MACRO_FEATURE_COVERAGE_BELOW_80_PERCENT')
    if fresh_oi_ratio is not None and fresh_oi_ratio < 0.80:
        flags.append('OPEN_INTEREST_FRESHNESS_BELOW_80_PERCENT')
    return {
        'ranges': [
            {'start_ms': int(start * 1000), 'end_ms': int(end * 1000)}
            for start, end in ranges
        ],
        'feature_minutes': _minute_summary(features),
        'bot_funnel': _bot_summary(bot_events),
        'data_quality': {
            'feature_seconds_observed': len(seconds),
            'feature_seconds_expected': expected,
            'feature_coverage_ratio': round(coverage, 6),
            'bot_events_observed': len(bot_events),
            'malformed_rows_skipped': malformed_features + malformed_bot,
            'quality_aware_feature_rows': len(quality_rows),
            'macro_complete_ratio': (
                round(macro_complete_ratio, 6)
                if macro_complete_ratio is not None else None
            ),
            'fresh_open_interest_ratio': (
                round(fresh_oi_ratio, 6)
                if fresh_oi_ratio is not None else None
            ),
            'flags': flags,
        },
    }


def _envelope(config, analysis_type, linkage, subject, context):
    body = {
        'contract': 'SHADOW_RESEARCH_ONLY_NO_TRADING_AUTHORITY',
        'analysis_type': analysis_type,
        'model': config.model,
        'prompt_version': config.prompt_version,
        'symbol': config.symbol,
        'linkage': linkage,
        'subject': subject,
        'market_context': context,
    }
    return body, canonical_hash(body)


def build_cycle_envelope(config, reader, cycle):
    ranges = cycle_time_ranges(cycle, config.window_seconds)
    if not ranges:
        raise ValueError('cycle has no usable timestamps')
    linkage = {
        'position_cycle_id': cycle.get('position_cycle_id'),
        'setup_id': cycle.get('setup_id'),
        'setup_generation': cycle.get('setup_generation'),
    }
    return _envelope(
        config, 'CYCLE_REVIEW', linkage, _selected_cycle(cycle),
        build_market_context(reader, ranges),
    )


def build_regime_envelope(config, reader, bucket_end_seconds):
    start = float(bucket_end_seconds - config.regime_seconds)
    end = float(bucket_end_seconds) - 0.001
    linkage = {'bucket_start_ms': int(start * 1000), 'bucket_end_ms': int(end * 1000)}
    subject = {'purpose': 'REGIME_AND_REJECTED_SETUP_FUNNEL_REVIEW'}
    return _envelope(
        config, 'REGIME_REVIEW', linkage, subject,
        build_market_context(reader, [(start, end)]),
    )
