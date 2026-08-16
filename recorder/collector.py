"""Public Binance USD-M data collectors; contains no trading capability."""

import asyncio
import logging
import time

import aiohttp
import orjson
import websockets

from recorder import SCHEMA_VERSION
from recorder.depth import DepthGap, LocalOrderBook


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

    @staticmethod
    def now_ms():
        return time.time_ns() // 1_000_000

    def emit(
        self, stream, payload, event_time_ms=None,
        sequence_start=None, sequence_end=None, previous_sequence=None,
    ):
        receive_time_ms = self.now_ms()
        event_time_ms = int(event_time_ms or receive_time_ms)
        source = (
            'smc2026' if stream.startswith('bot_')
            else 'recorder' if stream.startswith('recorder_')
            else 'binance_usdm'
        )
        record = {
            'schema_version': SCHEMA_VERSION,
            'code_version': self.code_version,
            'config_version': self.config_version,
            'source': source,
            'symbol': self.config.symbol,
            'stream': stream,
            'event_time_ms': event_time_ms,
            'receive_time_ms': receive_time_ms,
            'sequence_start': sequence_start,
            'sequence_end': sequence_end,
            'previous_sequence': previous_sequence,
            'payload': payload,
        }
        self.health.saw(stream, event_time_ms)
        published = self.store.publish(record)
        if published and self.feature_engine is not None:
            if stream == 'depth_diff':
                self.feature_engine.observe_depth_diff(record)
            else:
                self.feature_engine.process(record)
        return published

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

    async def _depth_snapshot(self):
        snapshot = await self._get_json(
            '/fapi/v1/depth', {'symbol': self.config.symbol, 'limit': 1000}
        )
        self.emit(
            'depth_snapshot', snapshot,
            sequence_end=int(snapshot['lastUpdateId']),
        )
        return snapshot

    async def _public_reader(self, ws, incoming):
        """Sole websocket reader; starts before REST snapshot to avoid depth gaps."""
        try:
            async for raw in ws:
                await incoming.put(('DATA', self.now_ms(), raw))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await incoming.put(('ERROR', self.now_ms(), exc))
        finally:
            await incoming.put(('EOF', self.now_ms(), None))

    async def public_loop(self):
        name = 'public_ws'
        while True:
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
                    self.health.connection(name, True)
                    incoming = asyncio.Queue(maxsize=50_000)
                    reader = asyncio.create_task(
                        self._public_reader(ws, incoming), name='public_socket_reader'
                    )
                    try:
                        # Chỉ lấy REST snapshot sau khi subscription depth thật sự
                        # hoạt động; reader vẫn tiếp tục buffer trong lúc REST chạy.
                        bootstrap = []
                        while True:
                            item = await asyncio.wait_for(incoming.get(), timeout=10.0)
                            bootstrap.append(item)
                            kind, _, raw = item
                            if kind == 'ERROR':
                                raise raw
                            if kind == 'EOF':
                                raise ConnectionError('PUBLIC_WEBSOCKET_EOF_BEFORE_DEPTH')
                            wrapper = orjson.loads(raw)
                            if str(wrapper.get('stream', '')).endswith('@depth@100ms'):
                                break
                        snapshot = await self._depth_snapshot()
                        book.reset(snapshot)
                        self.health.depth_synced = False
                        last_checkpoint = time.monotonic()
                        last_book_second = None
                        last_book_ticker_ms = 0
                        while True:
                            if bootstrap:
                                kind, receive_ms, raw = bootstrap.pop(0)
                            else:
                                kind, receive_ms, raw = await incoming.get()
                            if kind == 'ERROR':
                                raise raw
                            if kind == 'EOF':
                                raise ConnectionError('PUBLIC_WEBSOCKET_EOF')
                            wrapper = orjson.loads(raw)
                            stream_name = str(wrapper.get('stream', ''))
                            data = wrapper.get('data', {})
                            if stream_name.endswith('@depth@100ms'):
                                event_ms = int(data.get('E', receive_ms))
                                event_second = event_ms // 1000
                                if (
                                    last_book_second is not None
                                    and event_second != last_book_second
                                    and book.synced
                                    and self.feature_engine is not None
                                ):
                                    self.feature_engine.capture_book(last_book_second, book)
                                self.emit(
                                    'depth_diff', data, event_time_ms=event_ms,
                                    sequence_start=int(data['U']),
                                    sequence_end=int(data['u']),
                                    previous_sequence=int(data.get('pu', 0) or 0),
                                )
                                try:
                                    status = book.apply(data)
                                except DepthGap as exc:
                                    self.health.depth_gaps += 1
                                    self.health.depth_synced = False
                                    self.emit('recorder_health_event', {
                                        'component': 'depth',
                                        'event': 'SEQUENCE_GAP',
                                        'detail': str(exc),
                                    })
                                    raise
                                if status == 'APPLIED':
                                    last_book_second = event_second
                                    self.health.depth_synced = True
                                    self.health.depth_last_u = book.last_u
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
                                            sequence_end=book.last_u,
                                        )
                                        last_book_ticker_ms = event_ms
                                if (
                                    book.synced
                                    and time.monotonic() - last_checkpoint
                                    >= self.config.depth_checkpoint_interval
                                ):
                                    checkpoint = book.checkpoint()
                                    self.emit(
                                        'depth_checkpoint', checkpoint,
                                        sequence_end=book.last_u,
                                    )
                                    self.health.depth_checkpoints += 1
                                    last_checkpoint = time.monotonic()
                    finally:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
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
        while True:
            try:
                async with websockets.connect(
                    self.config.market_stream_url,
                    ping_interval=150,
                    ping_timeout=600,
                    close_timeout=5,
                    max_size=8 * 1024 * 1024,
                    max_queue=4096,
                ) as ws:
                    self.health.connection(name, True)
                    async for raw in ws:
                        receive_ms = self.now_ms()
                        wrapper = orjson.loads(raw)
                        stream_name = str(wrapper.get('stream', ''))
                        data = wrapper.get('data', {})
                        event_ms = int(data.get('E', receive_ms))
                        if stream_name.endswith('@aggTrade'):
                            trade_id = int(data.get('a', 0) or 0)
                            self.emit(
                                'agg_trade', data, event_time_ms=event_ms,
                                sequence_start=trade_id, sequence_end=trade_id,
                            )
                        elif '@kline_1m' in stream_name:
                            self.emit('kline_1m', data, event_time_ms=event_ms)
                        elif '@kline_15m' in stream_name:
                            self.emit('kline_15m', data, event_time_ms=event_ms)
                        elif '@markPrice' in stream_name:
                            self.emit('mark_price', data, event_time_ms=event_ms)
                        elif stream_name.endswith('@forceOrder'):
                            order = data.get('o', {}) or {}
                            liquidation_ms = int(order.get('T', event_ms) or event_ms)
                            self.emit(
                                'liquidation', data,
                                event_time_ms=liquidation_ms,
                                sequence_start=int(order.get('T', liquidation_ms) or liquidation_ms),
                                sequence_end=int(order.get('T', liquidation_ms) or liquidation_ms),
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.connection(name, False)
                self.health.reconnects[name] += 1
                self.health.error(name, exc)
                logging.warning('[RECORDER] market reconnect: %s', exc)
                await asyncio.sleep(2)

    async def macro_poll_loop(self):
        name = 'rest_macro'
        while True:
            started = time.monotonic()
            try:
                oi, premium = await asyncio.gather(
                    self._get_json(
                        '/fapi/v1/openInterest', {'symbol': self.config.symbol}
                    ),
                    self._get_json(
                        '/fapi/v1/premiumIndex', {'symbol': self.config.symbol}
                    ),
                )
                self.emit(
                    'open_interest', oi,
                    event_time_ms=int(oi.get('time', self.now_ms())),
                )
                self.emit(
                    'premium_index', premium,
                    event_time_ms=int(premium.get('time', self.now_ms())),
                )
                self.health.connection(name, True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.connection(name, False)
                self.health.errors[name] += 1
                self.health.error(name, exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self.config.oi_interval - elapsed))
