#!/usr/bin/env python3
"""Causal replay for the three locked Continuous V2 incidents.

Feature rows are admitted only when both event_time and receive_time are not
later than the evaluation timestamp. Future MFE/MAE never enters a snapshot.
"""

import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as parquet


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path('/home/ubuntu/smc2026_data')
DAY = '2026-08-11'
SHORT_0125_TARGET = 1786472700.0  # 2026-08-12 01:25 Asia/Ho_Chi_Minh
LONG_0246_CYCLE = 'pc_1786477572780_221070'
SHORT_0301_CYCLE = 'pc_1786478461211_636431'


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scorer = _load(
    'replay_continuous_v2_scorer',
    '2_suy_luan_mapping/tong_ket_chi_huy/cham_diem_continuous_v2.py',
)
executor = _load('replay_continuous_v2_executor', '3_thuc_thi/dat_lenh.py')


CONTINUOUS_FIELDS = (
    'continuous_m15', 'continuous_sweep_m1', 'continuous_breakout_m1',
    'continuous_footprint', 'continuous_persistent_flow',
    'continuous_zone_reaction', 'continuous_flow_divergence',
    'continuous_absorption_reaction', 'continuous_value_area_sweep',
)


def _event(direction=0.0, strength=0.0, quality=0.0, ts=0.0,
           source=None, dependencies=(), parent=None, ttl=20.0):
    return {
        'active': bool(strength), 'direction': direction,
        'strength': strength, 'quality': quality, 'ts': ts, 'ttl': ttl,
        'source_event_id': source, 'parent_event_id': parent,
        'dependency_families': list(dependencies),
    }


def _load_features(hours):
    rows = {}
    base = DATA_ROOT / 'raw' / 'parquet' / 'feature_1s' / DAY
    for hour in hours:
        path = base / f'{int(hour):02d}.parquet'
        if not path.exists():
            continue
        table = parquet.read_table(
            path, columns=['event_time_ms', 'receive_time_ms', 'payload_json']
        )
        for row in table.to_pylist():
            payload = json.loads(row['payload_json'])
            book = payload.get('book') or {}
            price = book.get('mid') or payload.get('last_trade_price')
            if not price:
                continue
            event_ts = float(row['event_time_ms']) / 1000.0
            receive_ts = float(row['receive_time_ms']) / 1000.0
            candidate = {
                'ts': event_ts, 'receive_ts': receive_ts,
                'price': float(price),
                'buy': float(payload.get('buy_qty', 0.0) or 0.0),
                'sell': float(payload.get('sell_qty', 0.0) or 0.0),
                'spread_bps': float(book.get('spread_bps', 0.0) or 0.0),
            }
            # If recorder overlap exists, the earliest causally available copy
            # is the only one admitted for this event-second.
            previous = rows.get(event_ts)
            if previous is None or receive_ts < previous['receive_ts']:
                rows[event_ts] = candidate
    return sorted(rows.values(), key=lambda item: item['ts'])


def _nearest_context(target):
    events = ROOT / '3_thuc_thi' / 'quan_ly_vi_the' / 'nhat_ky' / 'events.jsonl'
    best = None
    with events.open(encoding='utf-8', errors='ignore') as handle:
        for line in handle:
            if '"event":"DECISION_CONTEXT_M1"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('event') != 'DECISION_CONTEXT_M1':
                continue
            ts = float(event.get('ts', 0.0) or 0.0)
            distance = abs(ts - target)
            if distance <= 180.0 and (best is None or distance < best[0]):
                best = (distance, ts, event.get('payload') or {})
    if best is None:
        raise RuntimeError(f'no causal M1 context near {target}')
    return best[2]


def _available(rows, timestamp):
    return [
        row for row in rows
        if timestamp - 190.0 <= row['ts'] <= timestamp
        and row['receive_ts'] <= timestamp
    ]


def _snapshot(rows, timestamp, context, continuous=None):
    available = _available(rows, timestamp)
    if not available:
        raise RuntimeError(f'no causally available features at {timestamp}')
    current = available[-1]['price']
    atr = float(context.get('atr_1m', 0.0) or 0.0)
    vah = float(context.get('vah', 0.0) or 0.0)
    val = float(context.get('val', 0.0) or 0.0)
    horizons = {}
    for horizon in (15, 60, 180):
        window = [row for row in available if row['ts'] >= timestamp - horizon]
        prices = [row['price'] for row in window]
        buy = sum(row['buy'] for row in window)
        sell = sum(row['sell'] for row in window)
        total = buy + sell
        progress = (current - prices[0]) / atr if atr > 0.0 else 0.0
        expansion = (max(prices) - min(prices)) / atr if atr > 0.0 else 0.0
        horizons[str(horizon)] = {
            'price_progress_atr': progress,
            'range_expansion_atr': expansion,
            'price_efficiency': (
                min(1.0, abs(progress) / expansion) if expansion > 0.0 else 0.0
            ),
            'price_coverage_seconds': timestamp - window[0]['ts'],
            'flow_imbalance': (buy - sell) / total if total > 0.0 else 0.0,
            'flow_total': total,
            'flow_coverage_seconds': timestamp - window[0]['ts'],
            'acceptance_long': sum(price >= vah for price in prices) / len(prices),
            'acceptance_short': sum(price <= val for price in prices) / len(prices),
        }
    values = {
        'snapshot_time': timestamp,
        'best_bid': current - 0.05, 'best_ask': current + 0.05,
        'atr_1m': atr, 'poc': float(context.get('poc', 0.0) or 0.0),
        'vah': vah, 'val': val, 'vol_pct90': 5.0,
        'current_cvd_buy_3s': sum(row['buy'] for row in available[-3:]),
        'current_cvd_sell_3s': sum(row['sell'] for row in available[-3:]),
        'momentum_horizons': horizons,
    }
    for field in CONTINUOUS_FIELDS:
        values[field] = _event(ts=timestamp)
    values.update(continuous or {})
    return SimpleNamespace(**values), current


def _load_cycles():
    source = json.loads((
        ROOT / '3_thuc_thi' / 'quan_ly_vi_the' / 'nhat_ky' / 'cycles.json'
    ).read_text(encoding='utf-8'))
    return {row['position_cycle_id']: row for row in source.get('cycles', ())}


def replay_short_0125(rows):
    context = _nearest_context(SHORT_0125_TARGET)
    first = None
    first_ready = None
    trace = []
    setup = {
        'setup_id': 'replay-short-0125', 'generation': 1,
        'semantic_key': 'replay-short-0125',
        'mode': 'NEUTRAL-MOMENTUM', 'bias': 'SHORT',
        'zone': float(context['val']), 'kind': 'zone',
    }
    for timestamp in range(
        int(SHORT_0125_TARGET - 300), int(SHORT_0125_TARGET + 301)
    ):
        snapshot, price = _snapshot(rows, float(timestamp), context)
        result = scorer.score_continuous(snapshot, setup, {}, live=False)
        ready, ready_reason = scorer.entry_ready(setup, result, float(timestamp))
        if timestamp % 15 == 0:
            trace.append({
                'ts': timestamp, 'price': price,
                'momentum_state': result['momentum_state'],
                'trade_power': result['trade_power'],
                'floor': result['activation_floor'],
                'retest_fit': result['retest_fit'],
            })
        if result['activated'] and first is None:
            first = {'ts': timestamp, 'price': price, 'score': result}
        if ready and first_ready is None:
            first_ready = {
                'ts': timestamp, 'price': price, 'score': result,
                'reason': ready_reason,
            }
    end_rows = _available(rows, SHORT_0125_TARGET + 600)
    end_price = end_rows[-1]['price'] if end_rows else None
    return {
        'case': 'SHORT_01_25_VN', 'future_fields_used_as_features': False,
        'first_activation': None if first is None else {
            'ts': first['ts'],
            'utc': datetime.fromtimestamp(first['ts'], timezone.utc).isoformat(),
            'price': first['price'],
            'trade_power': first['score']['trade_power'],
            'floor': first['score']['activation_floor'],
            'size_pct': first['score']['target_notional_pct'],
            'entry_style': first['score']['entry_style_policy'],
            'momentum_state': first['score']['momentum_state'],
            'retest_fit': first['score']['retest_fit'],
        },
        'activated_before_target': bool(first and first['ts'] <= SHORT_0125_TARGET),
        'first_entry_ready': None if first_ready is None else {
            'ts': first_ready['ts'], 'price': first_ready['price'],
            'trade_power': first_ready['score']['trade_power'],
            'floor': first_ready['score']['activation_floor'],
            'size_pct': first_ready['score']['target_notional_pct'],
            'reason': first_ready['reason'],
        },
        'entry_ready_before_target': bool(
            first_ready and first_ready['ts'] <= SHORT_0125_TARGET
        ),
        'post_label_only': {'price_at_plus_10m': end_price},
        'trace_15s': trace,
    }


def replay_long_0251(rows, cycles):
    cycle = cycles[LONG_0246_CYCLE]
    score = cycle.get('continuous_score') or {}
    created = float(cycle['created_at'])
    signal = {'continuous_score': score}
    ttl = executor._passive_intent_ttl(signal)
    decision_bid = float(cycle.get('decision_price', 0.0) or 0.0) - 0.1
    candidates = [
        row for row in rows
        if created <= row['ts'] <= created + ttl
        and row['receive_ts'] <= row['ts'] + 5.0
        and row['price'] <= decision_bid
    ]
    fill = candidates[0] if candidates else None
    return {
        'case': 'LONG_02_51_VN', 'cycle_id': LONG_0246_CYCLE,
        'future_fields_used_as_features': False,
        'intent_started_at': created, 'intent_ttl_seconds': ttl,
        'legacy_two_second_terminal': cycle.get('abort_reason'),
        'v2_state_after_two_seconds': 'RETRY_WAIT',
        'counterfactual_mainnet_fill': None if fill is None else {
            'ts': fill['ts'], 'price': fill['price'],
            'seconds_after_intent': fill['ts'] - created,
        },
        'would_fill_inside_v2_ttl': bool(fill),
    }


def replay_short_0301(rows, cycles):
    cycle = cycles[SHORT_0301_CYCLE]
    timestamp = float(cycle['created_at'])
    context = _nearest_context(timestamp)
    # Locked causal evidence from the decision immediately before the losing
    # fill. Reversal labels share one price parent; BUY footprint is retained
    # as a full independent counterargument.
    continuous = {
        'continuous_sweep_m1': _event(
            -1.0, 1.0, 0.82, timestamp, 'sweep-short',
            ('PRICE_REACTION',), 'reversal-parent',
        ),
        'continuous_zone_reaction': _event(
            -1.0, 0.80, 0.80, timestamp, 'zone-short',
            ('PRICE_REACTION',), 'reversal-parent',
        ),
        'continuous_value_area_sweep': _event(
            -1.0, 1.0, 0.85, timestamp, 'va-short',
            ('PRICE_REACTION',), 'reversal-parent',
        ),
        'continuous_absorption_reaction': _event(
            1.0, 1.0, 1.0, timestamp, 'absorption-buy',
            ('DEPTH', 'AGGTRADE', 'PRICE_REACTION'), 'absorption-buy-parent',
        ),
        'continuous_footprint': _event(
            1.0, 0.825, 0.90, timestamp, 'footprint-buy', ('AGGTRADE',),
        ),
        'continuous_persistent_flow': _event(
            -1.0, 0.68, 0.70, timestamp, 'flow-short', ('AGGTRADE',),
        ),
    }
    snapshot, _ = _snapshot(rows, timestamp, context, continuous)
    result = scorer.score_continuous(snapshot, {
        'setup_id': cycle.get('setup_id'),
        'generation': int(cycle.get('setup_generation', 0) or 0),
        'semantic_key': cycle.get('setup_id'), 'mode': 'NEUTRAL-FADE',
        'bias': 'SHORT', 'zone': float(context['vah']), 'kind': 'zone',
    }, {}, live=False)
    return {
        'case': 'SHORT_LOSS_03_01_VN', 'cycle_id': SHORT_0301_CYCLE,
        'future_fields_used_as_features': False,
        'v1_trade_power': (cycle.get('continuous_score') or {}).get('trade_power'),
        'v2': {
            key: result.get(key) for key in (
                'score', 'confidence', 'activation', 'trade_power',
                'activation_floor', 'activated', 'target_notional_pct',
                'momentum_state', 'impulse_conflict',
            )
        },
        'causal_components': result['causal_components'],
        'post_label_only': {
            'shadow_net_pnl_bps': (cycle.get('shadow') or {}).get('net_pnl_bps'),
            'mfe_bps': (cycle.get('shadow') or {}).get('MFE_bps'),
            'mae_bps': (cycle.get('shadow') or {}).get('MAE_bps'),
        },
    }


def replay(data_root=DATA_ROOT):
    global DATA_ROOT
    DATA_ROOT = Path(data_root)
    rows = _load_features((17, 18, 19, 20))
    cycles = _load_cycles()
    return {
        'version': scorer.LIVE_VERSION,
        'causal_contract': 'event_time<=t AND receive_time<=t',
        'results': [
            replay_short_0125(rows),
            replay_long_0251(rows, cycles),
            replay_short_0301(rows, cycles),
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, default=DATA_ROOT)
    args = parser.parse_args()
    print(json.dumps(replay(args.data_root), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
