"""Public Binance USD-M data collectors; contains no trading capability."""

import asyncio
import logging
import time

import aiohttp
import orjson
import websockets

from loi_he_thong.market_event_contract import build_envelope
from recorder import SCHEMA_VERSION
from recorder.cash import (
    CashTradeBatcher, CausalClockEstimator, coinbase_time_ms,
    is_coinbase_live_match,
)
from recorder.depth import DepthGap, LocalOrderBook
from recorder.coinbase_l2 import CoinbaseL2Book, CoinbaseL2UpdateBatcher


class BinanceRecorder:
    def __init__(
        self, config, store, health, feature_engine=None,
        code_version='', config_version='',
    ):
        self.config = config
        self.store = store
        self.health = health
        self.feature_engine = feature_engine
        self.code_version = code_version
        self.config_version = config_version
        self.session = None
        self.decision_outcome_tracker = None
        self.wavefront_evaluator = None
        self.liquidity_response_analyzer = None
        self.spot_liquidity_response_analyzer = None
        self.coinbase_liquidity_response_analyzer = None
        self.causal_world_model = None
        self.cross_derivative_context = None
        self._causal_clocks = {}
        self._stream_epochs = {}
        self._wavefront_health_at_ms = 0
        self._liquidity_health_at_ms = 0
        self._shutdown_requested = False

    def request_shutdown(self):
        """Let hot receive queues exit even if task cancellation is delayed."""
        self._shutdown_requested = True

    @staticmethod
    def now_ms():
        return time.time_ns() // 1_000_000

    def _advance_epoch(self, *streams):
        for stream in streams:
            self._stream_epochs[str(stream)] = int(
                self._stream_epochs.get(str(stream), 0)
            ) + 1

    def _epoch(self, stream, payload=None):
        if str(stream) in self._stream_epochs:
            return int(self._stream_epochs[str(stream)])
        payload_epoch = (payload or {}).get('epoch')
        if payload_epoch is not None:
            return int(payload_epoch or 0)
        return int(self._stream_epochs.get(str(stream), 0))

    def emit(
        self, stream, payload, event_time_ms=None,
        sequence_start=None, sequence_end=None, previous_sequence=None,
        source=None, feed_features=True, feed_research=True,
        receive_time_ms=None, available_time_ms=None,
        receive_time_monotonic_ns=None, available_time_monotonic_ns=None,
        source_health='FRESH', payload_version=None, epoch=None,
    ):
        payload = dict(payload or {})
        receive_time_ms = int(receive_time_ms or self.now_ms())
        event_time_ms = int(event_time_ms or receive_time_ms)
        source = source or (
            'smc2026' if stream.startswith('bot_')
            else 'recorder' if stream.startswith('recorder_')
            else 'binance_usdm'
        )
        if (
            source in {
                'binance_usdm', 'binance_spot', 'coinbase_spot',
                'bybit_linear',
            }
            and 'clock_uncertainty_ms' not in payload
        ):
            clock = self._causal_clocks.setdefault(
                f'{source}:{stream}', CausalClockEstimator()
            )
            payload.update(clock.observe(event_time_ms, receive_time_ms))
        epoch = self._epoch(stream, payload) if epoch is None else int(epoch)
        clock_uncertainty = float(
            (payload or {}).get('clock_uncertainty_ms', 0.0) or 0.0
        )
        batch_uncertainty = max(0.0, float(
            (payload or {}).get('batch_available_time_ms', receive_time_ms)
            or receive_time_ms
        ) - float((payload or {}).get('bucket_end_ms', receive_time_ms) or receive_time_ms))
        envelope = build_envelope(
            source=source, stream=stream,
            exchange_event_time_ms=event_time_ms,
            receive_time_ms=receive_time_ms,
            available_time_ms=available_time_ms,
            receive_time_monotonic_ns=receive_time_monotonic_ns,
            available_time_monotonic_ns=available_time_monotonic_ns,
            epoch=epoch, sequence_start=sequence_start,
            sequence_end=sequence_end, previous_sequence=previous_sequence,
            source_health=source_health,
            payload_version=(
                payload_version
                or (payload or {}).get('signal_schema_version')
                or f'{stream.upper()}_V1'
            ),
            clock_offset_ms=(payload or {}).get('clock_offset_ms', 0.0),
            clock_jitter_ms=(payload or {}).get('clock_jitter_ms', 0.0),
            clock_uncertainty_ms=clock_uncertainty,
            batching_uncertainty_ms=batch_uncertainty,
            clock_valid=bool((payload or {}).get('clock_valid', True)),
            payload=payload,
        )
        record = {
            'schema_version': SCHEMA_VERSION,
            'code_version': self.code_version,
            'config_version': self.config_version,
            'source': source,
            'symbol': self.config.symbol,
            'stream': stream,
            **envelope,
            'payload': payload,
        }
        self.health.saw(stream, event_time_ms, envelope['available_time_ms'])
        published = self.store.publish(record)
        if published and feed_features and self.feature_engine is not None:
            if stream == 'depth_diff':
                self.feature_engine.observe_depth_diff(record)
            else:
                self.feature_engine.process(record)
        if (
            published and feed_research
            and self.decision_outcome_tracker is not None
        ):
            try:
                self.decision_outcome_tracker.observe(record)
            except Exception as exc:
                # Outcome research must never interrupt raw market recording.
                self.health.error('decision_outcomes', exc)
        if published and feed_research and self.wavefront_evaluator is not None:
            try:
                self.wavefront_evaluator.observe(record)
                if receive_time_ms - self._wavefront_health_at_ms >= 5_000:
                    self.health.wavefront_shadow = self.wavefront_evaluator.summary()
                    self._wavefront_health_at_ms = receive_time_ms
            except Exception as exc:
                # Wavefront is research-only and must never interrupt raw WAL.
                self.health.error('wavefront_shadow', exc)
        if (
            published and feed_research
            and self.liquidity_response_analyzer is not None
        ):
            try:
                self.liquidity_response_analyzer.observe(record)
                if receive_time_ms - self._liquidity_health_at_ms >= 5_000:
                    self.health.liquidity_response = (
                        self.liquidity_response_analyzer.summary()
                    )
                    self._liquidity_health_at_ms = receive_time_ms
            except Exception as exc:
                self.health.error('liquidity_response', exc)
        if (
            published and feed_research
            and self.spot_liquidity_response_analyzer is not None
        ):
            try:
                self.spot_liquidity_response_analyzer.observe(record)
            except Exception as exc:
                self.health.error('spot_liquidity_response', exc)
        if (
            published and feed_research
            and self.coinbase_liquidity_response_analyzer is not None
        ):
            try:
                self.coinbase_liquidity_response_analyzer.observe(record)
            except Exception as exc:
                self.health.error('coinbase_liquidity_response', exc)
        # The world model must see derived liquidity rows emitted with
        # feed_research=False.  It explicitly ignores its own output stream,
        # so this cannot recurse and it remains authority-free.
        if published and self.causal_world_model is not None:
            try:
                self.causal_world_model.observe(record)
                self.health.causal_world_model = self.causal_world_model.summary()
            except Exception as exc:
                self.health.error('causal_world_model', exc)
        # Bybit is a recorder-only mechanism witness. It sees raw records even
        # when feed_research=False, ignores its own output, and cannot affect
        # strategy direction or vetoes.
        if published and self.cross_derivative_context is not None:
            try:
                self.cross_derivative_context.observe(record)
                self.health.cross_derivative_context = (
                    self.cross_derivative_context.summary()
                )
            except Exception as exc:
                self.health.error('cross_derivative_context', exc)
        return published

    def _emit_cash_batch(
        self, stream, source, batch, previous_sequence, feed_features=True,
    ):
        if not batch:
            return previous_sequence
        batch = dict(batch)
        # The batch becomes observable only when this function runs.  Preserve
        # the logical bucket close separately; never backdate freshness to it.
        available_ms = int(self.now_ms())
        available_mono_ns = time.monotonic_ns()
        batch['bucket_close_ms'] = batch.get('bucket_end_ms')
        batch['batch_available_time_ms'] = available_ms
        receive_ms = int(batch.get('last_receive_time_ms') or available_ms)
        event_ms = int(batch.get('last_event_time_ms') or receive_ms)
        clock = self._causal_clocks.setdefault(source, CausalClockEstimator())
        batch.update(clock.observe(event_ms, receive_ms))
        first_id = batch.get('first_trade_id')
        last_id = batch.get('last_trade_id')
        gap = False
        if (
            previous_sequence is not None and first_id is not None
            and int(first_id) != int(previous_sequence) + 1
        ):
            gap = True
            self.health.sequence_gaps[stream] += 1
            self.health.last_error = {
                'component': stream,
                'message': (
                    f'SEQUENCE_GAP expected={int(previous_sequence) + 1} '
                    f'actual={int(first_id)}'
                ),
                'at_ms': self.now_ms(),
            }
            self._advance_epoch(stream)
            batch['sequence_status'] = 'GAP_NEW_EPOCH'
            batch['expected_first_trade_id'] = int(previous_sequence) + 1
        self.emit(
            stream, batch,
            event_time_ms=batch.get('last_event_time_ms'),
            sequence_start=int(first_id) if first_id is not None else None,
            sequence_end=int(last_id) if last_id is not None else None,
            # A new epoch has no valid predecessor. Preserve the mismatch in
            # payload rather than falsely bridging the sequence contract.
            previous_sequence=None if gap else previous_sequence,
            source=source,
            feed_features=feed_features,
            receive_time_ms=receive_ms,
            available_time_ms=available_ms,
            receive_time_monotonic_ns=batch.get(
                'last_receive_monotonic_ns'
            ),
            available_time_monotonic_ns=available_mono_ns,
            source_health='DEGRADED' if gap else 'FRESH',
        )
        return int(last_id) if last_id is not None else previous_sequence

    def _emit_coinbase_l2_batch(self, batch, book, product_id):
        if not batch:
            return False
        available_ms = int(self.now_ms())
        available_mono_ns = time.monotonic_ns()
        payload = dict(batch)
        payload.update({
            'product_id': product_id,
            'epoch': book.epoch,
            'batch_available_time_ms': available_ms,
            'ordered_lossless_changes': True,
        })
        receive_ms = int(payload.get('last_receive_time_ms') or available_ms)
        event_ms = int(payload.get('last_event_time_ms') or receive_ms)
        self.emit(
            'coinbase_spot_l2batch', payload,
            event_time_ms=event_ms,
            receive_time_ms=receive_ms,
            available_time_ms=available_ms,
            receive_time_monotonic_ns=payload.get(
                'last_receive_monotonic_ns'
            ),
            available_time_monotonic_ns=available_mono_ns,
            source='coinbase_spot', feed_features=False,
        )
        if self.coinbase_liquidity_response_analyzer is not None:
            self.coinbase_liquidity_response_analyzer.observe({
                'stream': 'coinbase_spot_depth20',
                'event_time_ms': event_ms,
                'receive_time_ms': receive_ms,
                'payload': book.checkpoint(20),
            })
        return True

    async def start_session(self):
        timeout = aiohttp.ClientTimeout(total=10)
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def close_session(self):
        if self.session is not None:
            await self.session.close()

    async def _get_json(self, path, params):
        async with self.session.get(self.config.rest_base + path, params=params) as response:
            response.raise_for_status()
            return await response.json()

    def _oi_poll_interval(self):
        """Recorder cadence is independent from live strategy state."""
        return max(5.0, float(self.config.oi_interval))

    async def public_loop(self):
        name = 'public_ws'
        while not self._shutdown_requested:
            book = LocalOrderBook()
            try:
                async with websockets.connect(
                    self.config.public_stream_url,
                    ping_interval=150,
                    ping_timeout=600,
                    close_timeout=5,
                    max_size=32 * 1024 * 1024,
                    max_queue=4096,
                ) as ws:
                    self._advance_epoch('depth_checkpoint', 'book_ticker')
                    self.health.connection(name, True)
                    self.health.depth_synced = False
                    last_book_ticker_ms = 0
                    last_book_second = None
                    last_checkpoint_ms = 0
                    if self.liquidity_response_analyzer is not None:
                        self.liquidity_response_analyzer.reset(
                            self.now_ms(), 'DEPTH_EPOCH_RESET'
                        )
                    async for raw in ws:
                        receive_ms = self.now_ms()
                        receive_mono_ns = time.monotonic_ns()
                        wrapper = orjson.loads(raw)
                        stream_name = str(wrapper.get('stream', ''))
                        data = wrapper.get('data', {}) or {}
                        if not stream_name.endswith('@depth20@100ms'):
                            continue
                        event_ms = int(data.get('E', receive_ms) or receive_ms)
                        partial = dict(data)
                        partial['partial'] = True
                        try:
                            status = book.apply_partial(partial)
                        except DepthGap as exc:
                            self.health.depth_gaps += 1
                            self.health.depth_synced = False
                            self.emit('recorder_health_event', {
                                'component': 'depth', 'event': 'SEQUENCE_GAP',
                                'detail': str(exc),
                            }, receive_time_ms=receive_ms,
                                receive_time_monotonic_ns=receive_mono_ns)
                            if self.liquidity_response_analyzer is not None:
                                self.liquidity_response_analyzer.reset(
                                    receive_ms, 'DEPTH_SEQUENCE_GAP'
                                )
                            raise
                        if status == 'APPLIED':
                            self.health.depth_synced = True
                            self.health.depth_last_u = book.last_u
                            if self.liquidity_response_analyzer is not None:
                                self.liquidity_response_analyzer.observe({
                                    'stream': 'depth_diff',
                                    'event_time_ms': event_ms,
                                    'receive_time_ms': receive_ms,
                                    'payload': partial,
                                })
                            second = event_ms // 1000
                            if second != last_book_second:
                                if self.feature_engine is not None:
                                    self.feature_engine.capture_book(second, book)
                                last_book_second = second
                            checkpoint_ms = int(
                                max(1.0, self.config.depth_checkpoint_interval) * 1000
                            )
                            if event_ms - last_checkpoint_ms >= checkpoint_ms:
                                self.emit(
                                    'depth_checkpoint', book.checkpoint(20),
                                    event_time_ms=event_ms,
                                    receive_time_ms=receive_ms,
                                    receive_time_monotonic_ns=receive_mono_ns,
                                    sequence_end=book.last_u,
                                    feed_features=False,
                                )
                                self.health.depth_checkpoints += 1
                                last_checkpoint_ms = event_ms
                            ticker = book.best_ticker()
                            if (
                                ticker is not None
                                and event_ms - last_book_ticker_ms >= int(
                                    self.config.book_ticker_interval * 1000
                                )
                            ):
                                ticker['E'] = event_ms
                                self.emit(
                                    'book_ticker', ticker,
                                    event_time_ms=event_ms,
                                    receive_time_ms=receive_ms,
                                    receive_time_monotonic_ns=receive_mono_ns,
                                    sequence_end=book.last_u,
                                )
                                last_book_ticker_ms = event_ms
                        # A socket with a permanently non-empty receive queue
                        # can make ``recv()`` complete synchronously for many
                        # iterations. Yield explicitly so SIGTERM, the writer
                        # and health tasks cannot be starved by depth bursts.
                        if self._shutdown_requested:
                            return
                        await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.connection(name, False)
                self.health.reconnects[name] += 1
                self.health.error(name, exc)
                logging.warning('[RECORDER] public reconnect: %s', exc)
                await asyncio.sleep(2)

    async def market_loop(self):
        name = 'market_ws'
        while not self._shutdown_requested:
            # A reconnect is a hard causal boundary.  A trade identifier from
            # the previous socket may be useful for diagnostics but must not
            # authorize continuity in the new epoch.
            previous_trade_id = None
            batcher = CashTradeBatcher(
                self.config.cash_batch_ms, track_nq=True
            )
            try:
                async with websockets.connect(
                    self.config.market_stream_url,
                    ping_interval=150,
                    ping_timeout=600,
                    close_timeout=5,
                    max_size=8 * 1024 * 1024,
                    max_queue=4096,
                ) as ws:
                    self._advance_epoch(
                        'futures_trade_100ms', 'liquidation', 'mark_price',
                    )
                    self.health.connection(name, True)
                    while not self._shutdown_requested:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=self.config.cash_batch_ms / 1000.0
                            )
                        except asyncio.TimeoutError:
                            previous_trade_id = self._emit_cash_batch(
                                'futures_trade_100ms', 'binance_usdm',
                                batcher.flush(), previous_trade_id,
                                feed_features=True,
                            )
                            continue
                        receive_ms = self.now_ms()
                        receive_mono_ns = time.monotonic_ns()
                        previous_trade_id = self._emit_cash_batch(
                            'futures_trade_100ms', 'binance_usdm',
                            batcher.flush_due(receive_ms), previous_trade_id,
                            feed_features=True,
                        )
                        wrapper = orjson.loads(raw)
                        stream_name = str(wrapper.get('stream', ''))
                        data = wrapper.get('data', {})
                        event_ms = int(data.get('E', receive_ms))
                        if stream_name.endswith('@aggTrade'):
                            trade_id = int(data.get('a', 0) or 0)
                            completed = batcher.push(
                                receive_time_ms=receive_ms,
                                event_time_ms=event_ms,
                                trade_id=trade_id,
                                price=data.get('p', 0.0),
                                qty=data.get('q', 0.0),
                                aggressive_buy=not bool(data.get('m')),
                                non_rpi_qty=(
                                    data.get('nq') if 'nq' in data else None
                                ),
                                receive_time_monotonic_ns=receive_mono_ns,
                            )
                            previous_trade_id = self._emit_cash_batch(
                                'futures_trade_100ms', 'binance_usdm',
                                completed, previous_trade_id,
                                feed_features=True,
                            )
                        elif '@kline_1m' in stream_name:
                            self.emit(
                                'kline_1m', data, event_time_ms=event_ms,
                                receive_time_ms=receive_ms,
                                receive_time_monotonic_ns=receive_mono_ns,
                            )
                        elif '@kline_15m' in stream_name:
                            self.emit(
                                'kline_15m', data, event_time_ms=event_ms,
                                receive_time_ms=receive_ms,
                                receive_time_monotonic_ns=receive_mono_ns,
                            )
                        elif '@markPrice' in stream_name:
                            self.emit(
                                'mark_price', data, event_time_ms=event_ms,
                                receive_time_ms=receive_ms,
                                receive_time_monotonic_ns=receive_mono_ns,
                            )
                        elif stream_name.endswith('@forceOrder'):
                            order = data.get('o', {}) or {}
                            liquidation_ms = int(order.get('T', event_ms) or event_ms)
                            self.emit(
                                'liquidation', data,
                                event_time_ms=liquidation_ms,
                                sequence_start=int(order.get('T', liquidation_ms) or liquidation_ms),
                                sequence_end=int(order.get('T', liquidation_ms) or liquidation_ms),
                                receive_time_ms=receive_ms,
                                receive_time_monotonic_ns=receive_mono_ns,
                            )
                        await asyncio.sleep(0)
            except asyncio.CancelledError:
                previous_trade_id = self._emit_cash_batch(
                    'futures_trade_100ms', 'binance_usdm',
                    batcher.flush(), previous_trade_id,
                    feed_features=True,
                )
                raise
            except Exception as exc:
                previous_trade_id = self._emit_cash_batch(
                    'futures_trade_100ms', 'binance_usdm',
                    batcher.flush(), previous_trade_id,
                    feed_features=True,
                )
                self.health.connection(name, False)
                self.health.reconnects[name] += 1
                self.health.error(name, exc)
                logging.warning('[RECORDER] market reconnect: %s', exc)
                await asyncio.sleep(2)

    async def binance_spot_loop(self):
        """Record exact Spot flow in 100-ms batches plus sampled BBO."""
        name = 'binance_spot_ws'
        while not self._shutdown_requested:
            previous_trade_id = None
            batcher = CashTradeBatcher(self.config.cash_batch_ms)
            last_ticker_ms = 0
            try:
                async with websockets.connect(
                    self.config.spot_stream_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=8 * 1024 * 1024,
                    max_queue=4096,
                ) as ws:
                    self._advance_epoch(
                        'binance_spot_trade_100ms', 'binance_spot_ticker',
                    )
                    self.health.connection(name, True)
                    if self.spot_liquidity_response_analyzer is not None:
                        self.spot_liquidity_response_analyzer.reset(
                            self.now_ms(), 'SPOT_DEPTH_EPOCH_RESET'
                        )
                    while not self._shutdown_requested:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=self.config.cash_batch_ms / 1000.0
                            )
                        except asyncio.TimeoutError:
                            previous_trade_id = self._emit_cash_batch(
                                'binance_spot_trade_100ms', 'binance_spot',
                                batcher.flush(), previous_trade_id,
                            )
                            continue
                        receive_ms = self.now_ms()
                        receive_mono_ns = time.monotonic_ns()
                        previous_trade_id = self._emit_cash_batch(
                            'binance_spot_trade_100ms', 'binance_spot',
                            batcher.flush_due(receive_ms), previous_trade_id,
                        )
                        wrapper = orjson.loads(raw)
                        stream_name = str(wrapper.get('stream', ''))
                        data = wrapper.get('data', {}) or {}
                        if stream_name.endswith('@aggTrade'):
                            trade_id = int(data.get('a', 0) or 0)
                            completed = batcher.push(
                                receive_time_ms=receive_ms,
                                event_time_ms=int(data.get('E', receive_ms) or receive_ms),
                                trade_id=trade_id,
                                price=data.get('p', 0.0),
                                qty=data.get('q', 0.0),
                                aggressive_buy=not bool(data.get('m')),
                                receive_time_monotonic_ns=receive_mono_ns,
                            )
                            previous_trade_id = self._emit_cash_batch(
                                'binance_spot_trade_100ms', 'binance_spot',
                                completed, previous_trade_id,
                            )
                        elif stream_name.endswith('@depth5@100ms'):
                            bids = data.get('bids', []) or []
                            asks = data.get('asks', []) or []
                            if not bids or not asks:
                                continue
                            if self.spot_liquidity_response_analyzer is not None:
                                # Consume every top-5 replacement in memory;
                                # store only event-conditioned derived response.
                                self.spot_liquidity_response_analyzer.observe({
                                    'stream': 'binance_spot_depth5',
                                    'event_time_ms': int(
                                        data.get('E', receive_ms) or receive_ms
                                    ),
                                    'receive_time_ms': receive_ms,
                                    'payload': {
                                        'bids': bids, 'asks': asks,
                                        'lastUpdateId': data.get('lastUpdateId'),
                                    },
                                })
                            if (
                                receive_ms - last_ticker_ms
                                >= int(self.config.cash_ticker_interval * 1000)
                            ):
                                self.emit(
                                    'binance_spot_ticker', {
                                        'bid': bids[0][0], 'bid_qty': bids[0][1],
                                        'ask': asks[0][0], 'ask_qty': asks[0][1],
                                        'update_id': data.get('lastUpdateId'),
                                    },
                                    event_time_ms=int(
                                        data.get('E', receive_ms) or receive_ms
                                    ),
                                    receive_time_ms=receive_ms,
                                    receive_time_monotonic_ns=receive_mono_ns,
                                    source='binance_spot', feed_features=True,
                                )
                                last_ticker_ms = receive_ms
                            else:
                                self.health.sampled_out[
                                    'binance_spot_ticker'
                                ] += 1
                        await asyncio.sleep(0)
            except asyncio.CancelledError:
                previous_trade_id = self._emit_cash_batch(
                    'binance_spot_trade_100ms', 'binance_spot',
                    batcher.flush(), previous_trade_id,
                )
                raise
            except Exception as exc:
                previous_trade_id = self._emit_cash_batch(
                    'binance_spot_trade_100ms', 'binance_spot',
                    batcher.flush(), previous_trade_id,
                )
                self.health.connection(name, False)
                self.health.reconnects[name] += 1
                self.health.error(name, exc)
                logging.warning('[RECORDER] Binance Spot reconnect: %s', exc)
                await asyncio.sleep(3)

    async def coinbase_spot_loop(self):
        """Record Coinbase matches, BBO and public L2 research evidence."""
        name = 'coinbase_spot_ws'
        subscribe = orjson.dumps({
            'type': 'subscribe',
            'product_ids': [self.config.coinbase_product],
            'channels': ['matches', 'ticker', 'level2_batch'],
        })
        while not self._shutdown_requested:
            previous_trade_id = None
            batcher = CashTradeBatcher(self.config.cash_batch_ms)
            l2_batcher = CoinbaseL2UpdateBatcher(self.config.cash_batch_ms)
            last_ticker_ms = 0
            l2 = CoinbaseL2Book()
            product_id = self.config.coinbase_product
            try:
                async with websockets.connect(
                    self.config.coinbase_ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=8 * 1024 * 1024,
                    max_queue=4096,
                ) as ws:
                    await ws.send(subscribe)
                    self._advance_epoch(
                        'coinbase_spot_trade_100ms', 'coinbase_spot_ticker',
                        'coinbase_spot_l2snapshot', 'coinbase_spot_l2batch',
                    )
                    self.health.connection(name, True)
                    if self.coinbase_liquidity_response_analyzer is not None:
                        self.coinbase_liquidity_response_analyzer.reset(
                            self.now_ms(), 'COINBASE_L2_EPOCH_RESET'
                        )
                    while not self._shutdown_requested:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=self.config.cash_batch_ms / 1000.0
                            )
                        except asyncio.TimeoutError:
                            previous_trade_id = self._emit_cash_batch(
                                'coinbase_spot_trade_100ms', 'coinbase_spot',
                                batcher.flush(), previous_trade_id,
                            )
                            self._emit_coinbase_l2_batch(
                                l2_batcher.flush_due(self.now_ms()), l2,
                                product_id,
                            )
                            continue
                        receive_ms = self.now_ms()
                        receive_mono_ns = time.monotonic_ns()
                        previous_trade_id = self._emit_cash_batch(
                            'coinbase_spot_trade_100ms', 'coinbase_spot',
                            batcher.flush_due(receive_ms), previous_trade_id,
                        )
                        data = orjson.loads(raw)
                        message_type = str(data.get('type', ''))
                        product_id = data.get('product_id') or product_id
                        if is_coinbase_live_match(message_type):
                            trade_id = int(data.get('trade_id', 0) or 0)
                            event_ms = coinbase_time_ms(data.get('time'), receive_ms)
                            # Coinbase reports the resting maker side.
                            aggressive_buy = str(data.get('side', '')).lower() == 'sell'
                            completed = batcher.push(
                                receive_time_ms=receive_ms,
                                event_time_ms=event_ms,
                                trade_id=trade_id,
                                price=data.get('price', 0.0),
                                qty=data.get('size', 0.0),
                                aggressive_buy=aggressive_buy,
                                receive_time_monotonic_ns=receive_mono_ns,
                            )
                            previous_trade_id = self._emit_cash_batch(
                                'coinbase_spot_trade_100ms', 'coinbase_spot',
                                completed, previous_trade_id,
                            )
                        elif message_type == 'ticker' and (
                            receive_ms - last_ticker_ms
                            >= int(self.config.cash_ticker_interval * 1000)
                        ):
                            sequence = int(data.get('sequence', 0) or 0)
                            self.emit(
                                'coinbase_spot_ticker', {
                                    'product_id': data.get('product_id'),
                                    'price': data.get('price'),
                                    'bid': data.get('best_bid'),
                                    'ask': data.get('best_ask'),
                                    'last_size': data.get('last_size'),
                                    'sequence': sequence,
                                },
                                event_time_ms=coinbase_time_ms(
                                    data.get('time'), receive_ms
                                ),
                                sequence_start=sequence or None,
                                sequence_end=sequence or None,
                                source='coinbase_spot', feed_features=True,
                                receive_time_ms=receive_ms,
                                receive_time_monotonic_ns=receive_mono_ns,
                            )
                            last_ticker_ms = receive_ms
                        elif message_type == 'ticker':
                            self.health.sampled_out['coinbase_spot_ticker'] += 1
                        elif message_type == 'snapshot':
                            self._emit_coinbase_l2_batch(
                                l2_batcher.flush(), l2, product_id,
                            )
                            l2.reset(data)
                            checkpoint = l2.checkpoint(20)
                            event_ms = receive_ms
                            self.emit(
                                'coinbase_spot_l2snapshot', {
                                    'product_id': data.get('product_id'),
                                    'epoch': l2.epoch,
                                    'bids': data.get('bids') or [],
                                    'asks': data.get('asks') or [],
                                },
                                event_time_ms=event_ms,
                                receive_time_ms=receive_ms,
                                receive_time_monotonic_ns=receive_mono_ns,
                                source='coinbase_spot', feed_features=False,
                            )
                            if self.coinbase_liquidity_response_analyzer is not None:
                                self.coinbase_liquidity_response_analyzer.observe({
                                    'stream': 'coinbase_spot_depth20',
                                    'event_time_ms': event_ms,
                                    'receive_time_ms': receive_ms,
                                    'payload': checkpoint,
                                })
                        elif message_type == 'l2update':
                            event_ms = coinbase_time_ms(
                                data.get('time'), receive_ms
                            )
                            completed_l2 = l2_batcher.push(
                                receive_ms, event_ms,
                                data.get('changes') or [],
                                receive_time_monotonic_ns=receive_mono_ns,
                            )
                            # Publish the closed bucket against the book state
                            # that existed before this new bucket's first
                            # update.  Applying first would leak one future L2
                            # event into the previous checkpoint.
                            self._emit_coinbase_l2_batch(
                                completed_l2, l2, product_id,
                            )
                            l2.apply(data)
                        await asyncio.sleep(0)
            except asyncio.CancelledError:
                previous_trade_id = self._emit_cash_batch(
                    'coinbase_spot_trade_100ms', 'coinbase_spot',
                    batcher.flush(), previous_trade_id,
                )
                self._emit_coinbase_l2_batch(
                    l2_batcher.flush(), l2, product_id,
                )
                raise
            except Exception as exc:
                previous_trade_id = self._emit_cash_batch(
                    'coinbase_spot_trade_100ms', 'coinbase_spot',
                    batcher.flush(), previous_trade_id,
                )
                self._emit_coinbase_l2_batch(
                    l2_batcher.flush(), l2, product_id,
                )
                self.health.connection(name, False)
                self.health.reconnects[name] += 1
                self.health.error(name, exc)
                logging.warning('[RECORDER] Coinbase Spot reconnect: %s', exc)
                await asyncio.sleep(5)

    async def macro_poll_loop(self):
        name = 'rest_macro'
        connected = False
        while not self._shutdown_requested:
            started = time.monotonic()
            try:
                (oi, oi_receive_ms, oi_receive_mono_ns), (
                    premium, premium_receive_ms, premium_receive_mono_ns,
                ) = await asyncio.gather(
                    self._get_json_observed(
                        '/fapi/v1/openInterest', {'symbol': self.config.symbol}
                    ),
                    self._get_json_observed(
                        '/fapi/v1/premiumIndex', {'symbol': self.config.symbol}
                    ),
                )
                # Both records become available to downstream consumers only
                # after gather completes, but each HTTP response has its own
                # receipt time.  Sharing the slower response timestamp made a
                # healthy OI reply appear several seconds late in canonical
                # replay and distorted cross-stream causal ordering.
                available_ms = self.now_ms()
                available_mono_ns = time.monotonic_ns()
                if not connected:
                    self._advance_epoch('open_interest', 'premium_index')
                oi_event_ms = int(
                    oi.get('time', oi_receive_ms) or oi_receive_ms
                )
                premium_event_ms = int(
                    premium.get('time', premium_receive_ms)
                    or premium_receive_ms
                )
                self.emit(
                    'open_interest', oi,
                    event_time_ms=oi_event_ms,
                    receive_time_ms=oi_receive_ms,
                    available_time_ms=available_ms,
                    receive_time_monotonic_ns=oi_receive_mono_ns,
                    available_time_monotonic_ns=available_mono_ns,
                )
                self.emit(
                    'premium_index', premium,
                    event_time_ms=premium_event_ms,
                    receive_time_ms=premium_receive_ms,
                    available_time_ms=available_ms,
                    receive_time_monotonic_ns=premium_receive_mono_ns,
                    available_time_monotonic_ns=available_mono_ns,
                )
                self.health.connection(name, True)
                connected = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                connected = False
                self.health.connection(name, False)
                self.health.errors[name] += 1
                self.health.error(name, exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self._oi_poll_interval() - elapsed))

    async def _get_json_observed(self, path, params=None):
        """Return payload plus the exact local receipt clocks for one REST call."""
        payload = await self._get_json(path, params)
        return payload, self.now_ms(), time.monotonic_ns()
