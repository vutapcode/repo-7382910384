"""Replay no-lookahead cho structural breakout xuất phát từ NEUTRAL.

Tool nghiên cứu này chỉ đọc WAL recorder. Nó không import Executor, không gọi
API tài khoản và không thể đặt lệnh. Entry replay là proxy bảo thủ: level phải
được M1 chạm và nến đó phải đóng giữ đúng phía trước khi bắt đầu đo kết quả.
"""

import argparse
import bisect
import importlib.util
import json
from pathlib import Path

import orjson


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = Path('/home/ubuntu/smc2026_data')
DEFAULT_HORIZONS = (900, 1800, 2700)


def _load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


structure_mod = _load_module(
    'neutral_replay_structure',
    '2_suy_luan_mapping/map-nen-offline/BOS_CHoCH.py',
)
atr_mod = _load_module(
    'neutral_replay_atr',
    '2_suy_luan_mapping/map-nen-offline/ATR.py',
)


def load_closed_klines(data_root, stream):
    """Load one final closed update per candle, keyed by Binance open time."""
    rows = {}
    root = Path(data_root) / 'raw' / 'wal' / stream
    for path in sorted(root.glob('*/*.jsonl')):
        with path.open('rb') as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = orjson.loads(line)
                candle = (record.get('payload', {}) or {}).get('k', {}) or {}
                if candle.get('x') is not True:
                    continue
                try:
                    open_time = int(candle['t'])
                    rows[open_time] = [
                        open_time,
                        float(candle['o']), float(candle['h']),
                        float(candle['l']), float(candle['c']),
                        float(candle.get('v', 0.0) or 0.0),
                        int(candle['T']),
                    ]
                except (KeyError, TypeError, ValueError):
                    continue
    return [rows[key] for key in sorted(rows)]


def evaluate_retest_outcome(
    candles_m1, confirm_close_ms, direction, level, horizons=DEFAULT_HORIZONS,
    round_trip_cost_bps=8.0,
):
    """Return M1 retest proxy and fee-aware forward paths without lookahead."""
    future = [row for row in candles_m1 if int(row[0]) > int(confirm_close_ms)]
    if not future:
        return {'entry': None, 'horizons': {}}

    entry_row = None
    for row in future:
        if int(row[0]) > int(confirm_close_ms) + max(horizons) * 1000:
            break
        high, low, close = float(row[2]), float(row[3]), float(row[4])
        touched = low <= level if direction == 'LONG' else high >= level
        held = close >= level if direction == 'LONG' else close <= level
        if touched and held:
            entry_row = row
            break
    if entry_row is None:
        return {'entry': None, 'horizons': {}}

    entry_time = int(entry_row[6])
    entry_price = float(level)
    sign = 1.0 if direction == 'LONG' else -1.0
    outcomes = {}
    for horizon in horizons:
        window = [
            row for row in future
            if entry_time < int(row[6]) <= entry_time + int(horizon) * 1000
        ]
        if not window or int(window[-1][6]) < entry_time + int(horizon) * 1000:
            continue
        favorable_prices = (
            [float(row[2]) for row in window]
            if direction == 'LONG' else [float(row[3]) for row in window]
        )
        adverse_prices = (
            [float(row[3]) for row in window]
            if direction == 'LONG' else [float(row[2]) for row in window]
        )
        close_price = float(window[-1][4])
        mfe_bps = max(
            sign * (price - entry_price) / entry_price * 10000.0
            for price in favorable_prices
        )
        mae_bps = min(
            sign * (price - entry_price) / entry_price * 10000.0
            for price in adverse_prices
        )
        close_bps = sign * (close_price - entry_price) / entry_price * 10000.0
        outcomes[str(horizon)] = {
            'close_bps': close_bps,
            'mfe_bps': mfe_bps,
            'mae_bps': mae_bps,
            'net_close_bps': close_bps - round_trip_cost_bps,
            'net_mfe_bps': mfe_bps - round_trip_cost_bps,
        }
    return {
        'entry': {
            'time_ms': entry_time,
            'price': entry_price,
            'model': 'M1_LEVEL_TOUCH_AND_CLOSE_HOLD_PROXY',
        },
        'horizons': outcomes,
    }


def replay(data_root, pivot_legs=5, round_trip_cost_bps=8.0):
    m15 = load_closed_klines(data_root, 'kline_15m')
    m1 = load_closed_klines(data_root, 'kline_1m')
    m1_close_times = [int(row[6]) for row in m1]
    signals = []

    for index in range(max(2 * pivot_legs + 1, 15), len(m15)):
        history = m15[:index + 1]
        confirm_ms = int(history[-1][6])
        m1_end = bisect.bisect_right(m1_close_times, confirm_ms)
        atr = atr_mod.tinh_atr_1m(m1[max(0, m1_end - 100):m1_end])
        structure = structure_mod.get_macro_structure(
            history, pivot_legs=pivot_legs, break_buffer=atr,
            now_ms=confirm_ms,
        )
        transition = structure.get('transition')
        if (
            transition not in (
                'NEUTRAL_TRANSITION_BULLISH',
                'NEUTRAL_TRANSITION_BEARISH',
            )
            or int(structure.get('break_streak', 0) or 0) != 2
        ):
            continue
        direction = 'LONG' if transition.endswith('BULLISH') else 'SHORT'
        level = float(structure.get('broken_level', 0.0) or 0.0)
        outcome = evaluate_retest_outcome(
            m1, confirm_ms, direction, level,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        signals.append({
            'confirmation_time_ms': confirm_ms,
            'direction': direction,
            'broken_level': level,
            'confirmation_close': float(structure.get('last_close', 0.0)),
            'atr_1m': atr,
            **outcome,
        })

    summary = {
        'closed_m15_candles': len(m15),
        'closed_m1_candles': len(m1),
        'confirmed_neutral_breakouts': len(signals),
        'retest_entries': sum(item['entry'] is not None for item in signals),
        'round_trip_cost_bps': round_trip_cost_bps,
        'horizons': {},
    }
    for horizon in DEFAULT_HORIZONS:
        key = str(horizon)
        available = [
            item['horizons'][key] for item in signals
            if key in item['horizons']
        ]
        summary['horizons'][key] = {
            'samples': len(available),
            'net_close_winners': sum(row['net_close_bps'] > 0 for row in available),
            'net_mfe_positive': sum(row['net_mfe_bps'] > 0 for row in available),
            'mean_net_close_bps': (
                sum(row['net_close_bps'] for row in available) / len(available)
                if available else None
            ),
            'mean_net_mfe_bps': (
                sum(row['net_mfe_bps'] for row in available) / len(available)
                if available else None
            ),
            'worst_mae_bps': (
                min(row['mae_bps'] for row in available) if available else None
            ),
        }
    return {
        'summary': summary,
        'signals': signals,
        'limitations': [
            'Recorder history is short; results are diagnostic, not calibration.',
            'M1 retest is a candle proxy and does not assume queue priority.',
            'Fee gate and live depth/slippage remain mandatory at execution.',
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Replay confirmed NEUTRAL structural breakouts at 15/30/45m'
    )
    parser.add_argument('--data-root', default=str(DEFAULT_DATA_ROOT))
    parser.add_argument('--pivot-legs', type=int, default=5)
    parser.add_argument('--round-trip-cost-bps', type=float, default=8.0)
    args = parser.parse_args(argv)
    result = replay(
        args.data_root, pivot_legs=args.pivot_legs,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
