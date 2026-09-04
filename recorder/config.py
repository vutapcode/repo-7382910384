"""Configuration for the read-only Binance recorder."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecorderConfig:
    symbol: str = os.getenv('SMC_RECORDER_SYMBOL', 'BTCUSDT').upper()
    data_root: Path = Path(
        os.getenv('SMC_RECORDER_DATA_ROOT', '/home/ubuntu/smc2026_data')
    )
    queue_max: int = int(os.getenv('SMC_RECORDER_QUEUE_MAX', '20000'))
    batch_max: int = int(os.getenv('SMC_RECORDER_BATCH_MAX', '1000'))
    # Two-second durable batches halve multi-stream fsync churn. Raw market
    # events remain append-only; only the crash-loss window changes by 1 sec.
    flush_interval: float = float(os.getenv('SMC_RECORDER_FLUSH_SECONDS', '2.0'))
    health_interval: float = float(os.getenv('SMC_RECORDER_HEALTH_SECONDS', '5'))
    retention_hours: int = int(os.getenv('SMC_RECORDER_RETENTION_HOURS', '24'))
    retention_interval: float = float(
        os.getenv('SMC_RECORDER_RETENTION_SECONDS', '60')
    )
    oi_interval: float = float(os.getenv('SMC_RECORDER_OI_SECONDS', '15'))
    oi_pressure_interval: float = float(
        os.getenv('SMC_RECORDER_OI_PRESSURE_SECONDS', '5')
    )
    depth_checkpoint_interval: float = float(
        os.getenv('SMC_RECORDER_DEPTH_CHECKPOINT_SECONDS', '60')
    )
    book_ticker_interval: float = float(
        os.getenv('SMC_RECORDER_BOOK_TICKER_SECONDS', '0.25')
    )
    cycles_snapshot_interval: float = float(
        os.getenv('SMC_RECORDER_CYCLES_SECONDS', '10')
    )
    decision_poll_interval: float = float(
        os.getenv('SMC_RECORDER_DECISION_POLL_SECONDS', '1.0')
    )
    feature_lateness_seconds: int = int(
        os.getenv('SMC_RECORDER_FEATURE_LATENESS_SECONDS', '5')
    )
    cash_batch_ms: int = int(os.getenv('SMC_RECORDER_CASH_BATCH_MS', '100'))
    cash_ticker_interval: float = float(
        os.getenv('SMC_RECORDER_CASH_TICKER_SECONDS', '0.25')
    )
    coinbase_product: str = os.getenv(
        'SMC_RECORDER_COINBASE_PRODUCT', 'BTC-USD'
    ).upper()
    bybit_research_enabled: bool = os.getenv(
        'WSTRADE_BYBIT_RESEARCH', '1'
    ).strip().lower() not in ('0', 'false', 'no', 'off')
    bybit_symbol: str = os.getenv(
        'WSTRADE_BYBIT_SYMBOL', 'BTCUSDT'
    ).upper()
    bot_runtime_path: Path = Path(os.getenv(
        'WSTRADE_BOT_HEALTH_PATH',
        '/home/ubuntu/smc2026_data/health/bot_runtime.json',
    ))
    wavefront_enabled: bool = os.getenv(
        'WSTRADE_WAVEFRONT_SHADOW', '1'
    ).strip().lower() not in ('0', 'false', 'no', 'off')
    cpu_status_path: Path = Path(os.getenv(
        'WSTRADE_CPU_HEALTH_PATH',
        '/home/ubuntu/smc2026_data/health/cpu_status.json',
    ))
    rest_base: str = 'https://fapi.binance.com'
    public_ws_base: str = 'wss://fstream.binance.com/public/stream?streams='
    market_ws_base: str = 'wss://fstream.binance.com/market/stream?streams='
    spot_ws_base: str = 'wss://stream.binance.com:9443/stream?streams='
    coinbase_ws_url: str = 'wss://ws-feed.exchange.coinbase.com'
    bybit_linear_ws_url: str = 'wss://stream.bybit.com/v5/public/linear'
    journal_events_path: Path = Path(
        os.getenv(
            'SMC_JOURNAL_EVENTS_PATH',
            '/home/ubuntu/.local/state/smc2026/mainnet_shadow/events.jsonl',
        )
    )
    journal_cycles_path: Path = Path(
        os.getenv(
            'SMC_JOURNAL_CYCLES_PATH',
            '/home/ubuntu/.local/state/smc2026/mainnet_shadow/cycles.json',
        )
    )

    @property
    def symbol_lower(self):
        return self.symbol.lower()

    @property
    def public_stream_url(self):
        # Best bid/ask được dựng từ cùng depth 100ms để tránh phải giải mã
        # hàng trăm bookTicker/giây trong một tiến trình quan sát độc lập.
        streams = f'{self.symbol_lower}@depth20@100ms'
        return self.public_ws_base + streams

    @property
    def market_stream_url(self):
        streams = '/'.join((
            f'{self.symbol_lower}@aggTrade',
            f'{self.symbol_lower}@markPrice@1s',
            f'{self.symbol_lower}@forceOrder',
        ))
        return self.market_ws_base + streams

    @property
    def spot_stream_url(self):
        streams = '/'.join((
            f'{self.symbol_lower}@aggTrade',
            f'{self.symbol_lower}@depth5@100ms',
        ))
        return self.spot_ws_base + streams
