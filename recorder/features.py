"""Low-cost event-time one-second feature factory for offline research."""

import time
import uuid
from collections import defaultdict, deque

from recorder import SCHEMA_VERSION
from loi_he_thong.market_event_contract import available_time_ms


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class FeatureEngine:
    """Aggregate O(1) per event; order-book bands are sampled only once/second."""

    def __init__(self, config, publish, health, code_version, config_version):
        self.config = config
        self.publish = publish
        self.health = health
        self.code_version = code_version
        self.config_version = config_version
        self.buckets = {}
        self.book_by_second = {}
        self.max_second = None
        self.flushed_through = None
        self.cvd_btc = 0.0
        self.cvd_window = deque()
        self.cvd_60s_btc = 0.0
        self.last_oi = None
        self.last_oi_event_ms = None
        self.latest_macro = {}
        self.latest_macro_meta = {}
        # A logical feature is keyed by (symbol, second).  The run id makes
        # overlapping recorder instances/restarts visible instead of silently
        # mixing two process-lifetime CVD series in the research dataset.
        self.recorder_run_id = uuid.uuid4().hex

    @staticmethod
    def _new_bucket(second):
        return {
            'bucket_start_ms': second * 1000,
            'bucket_end_ms': second * 1000 + 999,
            'event_counts': defaultdict(int),
            'agg_trade_count': 0,
            'first_trade_price': None,
            'last_trade_price': None,
            'trade_high': None,
            'trade_low': None,
            'buy_qty': 0.0,
            'sell_qty': 0.0,
            'buy_quote': 0.0,
            'sell_quote': 0.0,
            'cash_flow': {
                'binance_spot': {
                    'trade_count': 0, 'buy_qty': 0.0, 'sell_qty': 0.0,
                    'buy_quote': 0.0, 'sell_quote': 0.0,
                    'first_price': None, 'last_price': None,
                    'high': None, 'low': None,
                },
                'coinbase_spot': {
                    'trade_count': 0, 'buy_qty': 0.0, 'sell_qty': 0.0,
                    'buy_quote': 0.0, 'sell_quote': 0.0,
                    'first_price': None, 'last_price': None,
                    'high': None, 'low': None,
                },
            },
            'cash_bbo': {},
            'spread_samples': 0,
            'spread_bps_sum': 0.0,
            'spread_bps_max': 0.0,
            'mid_open': None,
            'mid_close': None,
            'mid_high': None,
            'mid_low': None,
            'liquidation_count': 0,
            'long_liquidation_qty': 0.0,
            'short_liquidation_qty': 0.0,
            'long_liquidation_quote': 0.0,
            'short_liquidation_quote': 0.0,
            'delay_samples': 0,
            'delay_ms_sum': 0.0,
            'delay_ms_max': 0.0,
            'macro': {},
            'macro_meta': {},
        }

    def _update_macro(self, field, value, event_ms, receive_ms, stream):
        previous = self.latest_macro_meta.get(field)
        if previous is not None and event_ms < previous['event_time_ms']:
            return False
        self.latest_macro[field] = value
        self.latest_macro_meta[field] = {
            'event_time_ms': int(event_ms),
            'receive_time_ms': int(receive_ms),
            'stream': stream,
        }
        return True

    def _observe_macro(self, stream, payload, event_ms, receive_ms):
        """Update slow state before event-time rejection so REST lag is usable.

        Binance REST macro timestamps can arrive after the global feature
        watermark.  They must not reopen an already emitted second, but their
        latest value can safely be forward-filled into later seconds.
        """
        updates = {}
        if stream == 'open_interest':
            current = _f(payload.get('openInterest'))
            if current <= 0.0:
                return updates
            if self.last_oi_event_ms is not None and event_ms < self.last_oi_event_ms:
                return updates
            change = current - self.last_oi if self.last_oi is not None else None
            values = {
                'open_interest': current,
                'open_interest_change': change,
            }
            for field, value in values.items():
                if self._update_macro(field, value, event_ms, receive_ms, stream):
                    updates[field] = value
            self.last_oi = current
            self.last_oi_event_ms = int(event_ms)
        elif stream in ('premium_index', 'mark_price'):
            for source, target in (
                ('markPrice', 'mark_price'), ('indexPrice', 'index_price'),
                # WebSocket @markPrice uses compact field names; REST
                # premiumIndex uses the verbose names above.
                ('p', 'mark_price'), ('i', 'index_price'),
                ('lastFundingRate', 'funding_rate'), ('r', 'funding_rate'),
            ):
                if payload.get(source) is None:
                    continue
                value = _f(payload.get(source))
                if self._update_macro(target, value, event_ms, receive_ms, stream):
                    updates[target] = value
        return updates

    def capture_book(self, second, book):
        metrics = book.microstructure()
        if metrics is not None:
            self.book_by_second[int(second)] = metrics

    def observe_depth_diff(self, record):
        """Count raw depth cheaply; book samples drive normal bucket flushing.

        Every depth row is still durably written.  Avoiding the generic feature
        path here removes payload parsing and a bucket scan at 10 Hz without
        changing the one-second book feature.
        """
        event_ms = int(record.get('event_time_ms', 0) or 0)
        if event_ms <= 0:
            return
        receive_ms = available_time_ms(record)
        second = event_ms // 1000
        if self.flushed_through is not None and second <= self.flushed_through:
            self.health.sampled_out['feature_event_too_late'] += 1
            self.health.sampled_out['feature_event_too_late.depth_diff'] += 1
            if hasattr(self.health, 'late_event'):
                self.health.late_event('depth_diff', receive_ms - event_ms)
            return
        self.max_second = second if self.max_second is None else max(
            self.max_second, second
        )
        bucket = self.buckets.setdefault(second, self._new_bucket(second))
        bucket['event_counts']['depth_diff'] += 1
        delay = max(0, receive_ms - event_ms)
        bucket['delay_samples'] += 1
        bucket['delay_ms_sum'] += delay
        bucket['delay_ms_max'] = max(bucket['delay_ms_max'], delay)

    def _observe_trade(self, bucket, payload):
        price = _f(payload.get('p'))
        qty = _f(payload.get('q'))
        quote = price * qty
        is_sell = bool(payload.get('m'))
        bucket['agg_trade_count'] += 1
        bucket['first_trade_price'] = (
            price if bucket['first_trade_price'] is None else bucket['first_trade_price']
        )
        bucket['last_trade_price'] = price
        bucket['trade_high'] = price if bucket['trade_high'] is None else max(bucket['trade_high'], price)
        bucket['trade_low'] = price if bucket['trade_low'] is None else min(bucket['trade_low'], price)
        if is_sell:
            bucket['sell_qty'] += qty
            bucket['sell_quote'] += quote
        else:
            bucket['buy_qty'] += qty
            bucket['buy_quote'] += quote

    @staticmethod
    def _observe_trade_batch(bucket, payload, venue=None):
        target = bucket if venue is None else bucket['cash_flow'][venue]
        count = max(0, int(payload.get('trade_count', 0) or 0))
        target['trade_count' if venue is not None else 'agg_trade_count'] += count
        for field in ('buy_qty', 'sell_qty', 'buy_quote', 'sell_quote'):
            target[field] += _f(payload.get(field))
        first = _f(payload.get('first_price'), None)
        last = _f(payload.get('last_price'), None)
        high = _f(payload.get('high'), None)
        low = _f(payload.get('low'), None)
        first_field = 'first_price' if venue is not None else 'first_trade_price'
        last_field = 'last_price' if venue is not None else 'last_trade_price'
        high_field = 'high' if venue is not None else 'trade_high'
        low_field = 'low' if venue is not None else 'trade_low'
        if first is not None and target[first_field] is None:
            target[first_field] = first
        if last is not None:
            target[last_field] = last
        if high is not None:
            target[high_field] = high if target[high_field] is None else max(
                target[high_field], high
            )
        if low is not None:
            target[low_field] = low if target[low_field] is None else min(
                target[low_field], low
            )

    @staticmethod
    def _observe_cash_ticker(bucket, payload, venue):
        bid = _f(payload.get('bid', payload.get('b')))
        ask = _f(payload.get('ask', payload.get('a')))
        if bid <= 0.0 or ask <= 0.0:
            return
        mid = (bid + ask) / 2.0
        previous = bucket['cash_bbo'].get(venue, {})
        bucket['cash_bbo'][venue] = {
            'bid': bid, 'ask': ask, 'mid': mid,
            'spread_bps': (ask - bid) / mid * 10000.0,
            'samples': int(previous.get('samples', 0) or 0) + 1,
        }

    @staticmethod
    def _observe_ticker(bucket, payload):
        bid = _f(payload.get('b'))
        ask = _f(payload.get('a'))
        if bid <= 0.0 or ask <= 0.0:
            return
        mid = (bid + ask) / 2.0
        spread = (ask - bid) / mid * 10000.0
        bucket['spread_samples'] += 1
        bucket['spread_bps_sum'] += spread
        bucket['spread_bps_max'] = max(bucket['spread_bps_max'], spread)
        bucket['mid_open'] = mid if bucket['mid_open'] is None else bucket['mid_open']
        bucket['mid_close'] = mid
        bucket['mid_high'] = mid if bucket['mid_high'] is None else max(bucket['mid_high'], mid)
        bucket['mid_low'] = mid if bucket['mid_low'] is None else min(bucket['mid_low'], mid)

    @staticmethod
    def _observe_liquidation(bucket, payload):
        order = payload.get('o', payload) or {}
        side = str(order.get('S', '')).upper()
        qty = _f(order.get('z') or order.get('q'))
        price = _f(order.get('ap') or order.get('p'))
        quote = qty * price
        bucket['liquidation_count'] += 1
        if side == 'SELL':
            bucket['long_liquidation_qty'] += qty
            bucket['long_liquidation_quote'] += quote
        elif side == 'BUY':
            bucket['short_liquidation_qty'] += qty
            bucket['short_liquidation_quote'] += quote

    def process(self, record):
        stream = record.get('stream', '')
        if stream in ('feature_1s', 'recorder_health_event') or stream.startswith('bot_'):
            return
        event_ms = int(record.get('event_time_ms', 0) or 0)
        if event_ms <= 0:
            return
        receive_ms = available_time_ms(record)
        payload = record.get('payload', {}) or {}
        macro_updates = {}
        if stream in ('open_interest', 'premium_index', 'mark_price'):
            macro_updates = self._observe_macro(
                stream, payload, event_ms, receive_ms
            )
        second = event_ms // 1000
        if self.flushed_through is not None and second <= self.flushed_through:
            self.health.sampled_out['feature_event_too_late'] += 1
            self.health.sampled_out[f'feature_event_too_late.{stream}'] += 1
            if hasattr(self.health, 'late_event'):
                self.health.late_event(stream, receive_ms - event_ms)
            if macro_updates:
                self.health.sampled_out[f'feature_macro_forward_fill.{stream}'] += 1
            return
        self.max_second = second if self.max_second is None else max(self.max_second, second)
        bucket = self.buckets.setdefault(second, self._new_bucket(second))
        bucket['event_counts'][stream] += 1
        delay = max(0, receive_ms - event_ms)
        bucket['delay_samples'] += 1
        bucket['delay_ms_sum'] += delay
        bucket['delay_ms_max'] = max(bucket['delay_ms_max'], delay)
        if stream == 'agg_trade':
            self._observe_trade(bucket, payload)
        elif stream == 'futures_trade_100ms':
            self._observe_trade_batch(bucket, payload)
        elif stream == 'book_ticker':
            self._observe_ticker(bucket, payload)
        elif stream in (
            'binance_spot_trade_100ms', 'coinbase_spot_trade_100ms'
        ):
            venue = 'binance_spot' if stream.startswith('binance_') else 'coinbase_spot'
            self._observe_trade_batch(bucket, payload, venue=venue)
        elif stream in ('binance_spot_ticker', 'coinbase_spot_ticker'):
            venue = 'binance_spot' if stream.startswith('binance_') else 'coinbase_spot'
            self._observe_cash_ticker(bucket, payload, venue)
        elif stream == 'liquidation':
            self._observe_liquidation(bucket, payload)
        if macro_updates:
            bucket['macro'].update(macro_updates)
            bucket['macro_meta'].update({
                field: dict(self.latest_macro_meta[field])
                for field in macro_updates
            })
        self._flush_ready()

    def _flush_ready(self, force=False):
        if self.max_second is None:
            return
        cutoff = self.max_second if force else self.max_second - self.config.feature_lateness_seconds
        ready = sorted(second for second in self.buckets if second <= cutoff)
        for second in ready:
            bucket = self.buckets.pop(second)
            payload = dict(bucket)
            payload['event_counts'] = dict(bucket['event_counts'])
            samples = int(bucket['spread_samples'])
            payload['spread_bps_mean'] = (
                bucket['spread_bps_sum'] / samples if samples else None
            )
            delay_samples = int(bucket['delay_samples'])
            payload['delay_ms_mean'] = (
                bucket['delay_ms_sum'] / delay_samples if delay_samples else None
            )
            payload['trade_delta_qty'] = bucket['buy_qty'] - bucket['sell_qty']
            payload['cash_flow'] = {
                venue: {
                    **dict(values),
                    'trade_delta_qty': values['buy_qty'] - values['sell_qty'],
                }
                for venue, values in bucket['cash_flow'].items()
            }
            self.cvd_btc += payload['trade_delta_qty']
            payload['cvd_btc'] = self.cvd_btc
            self.cvd_window.append((second, payload['trade_delta_qty']))
            self.cvd_60s_btc += payload['trade_delta_qty']
            while self.cvd_window and self.cvd_window[0][0] <= second - 60:
                _, expired_delta = self.cvd_window.popleft()
                self.cvd_60s_btc -= expired_delta
            payload['cvd_btc_60s'] = self.cvd_60s_btc
            payload['book'] = self.book_by_second.pop(second, None)
            macro = dict(bucket['macro'])
            macro_meta = dict(bucket['macro_meta'])
            for field, meta in self.latest_macro_meta.items():
                # Never leak a macro observation backwards into an earlier
                # event-time bucket just because it arrived before flushing.
                if meta['event_time_ms'] > payload['bucket_end_ms']:
                    continue
                current = macro_meta.get(field)
                if current is None or meta['event_time_ms'] >= current['event_time_ms']:
                    macro[field] = self.latest_macro.get(field)
                    macro_meta[field] = dict(meta)
            stale_after_ms = max(10_000, int(self.config.oi_interval * 2_000))
            macro_age_ms = {
                field: max(0, payload['bucket_end_ms'] - meta['event_time_ms'])
                for field, meta in macro_meta.items()
            }
            payload['macro'] = macro
            payload['macro_age_ms'] = macro_age_ms
            payload['macro_fresh'] = {
                field: age <= stale_after_ms
                for field, age in macro_age_ms.items()
            }
            payload['macro_complete'] = all(
                field in macro for field in ('open_interest', 'mark_price')
            )
            payload['recorder_run_id'] = self.recorder_run_id
            payload['feature_identity'] = f'{self.config.symbol}:{second}'
            payload.pop('macro_meta', None)
            record = {
                'schema_version': SCHEMA_VERSION,
                'code_version': self.code_version,
                'config_version': self.config_version,
                'source': 'recorder',
                'symbol': self.config.symbol,
                'stream': 'feature_1s',
                'event_time_ms': payload['bucket_end_ms'],
                'receive_time_ms': time.time_ns() // 1_000_000,
                'sequence_start': second,
                'sequence_end': second,
                'previous_sequence': second - 1,
                'payload': payload,
            }
            self.health.saw('feature_1s', record['event_time_ms'])
            self.publish(record)
            self.flushed_through = (
                second if self.flushed_through is None
                else max(self.flushed_through, second)
            )
        stale_books = [second for second in self.book_by_second if second < cutoff - 5]
        for second in stale_books:
            self.book_by_second.pop(second, None)

    def close(self):
        self._flush_ready(force=True)
