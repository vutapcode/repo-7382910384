"""Causal counterfactual labels from Mainnet recorder parquet.

Touch alone never implies a maker fill: require trade-through or enough
aggressor quantity at the quote to consume the recorded queue ahead.
"""

import argparse
import bisect
import datetime
import json
import math
from pathlib import Path

import pyarrow.parquet as pq


def _payloads(files, start_ms, end_ms):
    rows = []
    for path in files:
        if path.suffix == '.parquet':
            table = pq.read_table(
                path, filters=[('event_time_ms', '>=', start_ms), ('event_time_ms', '<=', end_ms)]
            )
            source_rows = table.to_pylist()
            for row in source_rows:
                try:
                    payload = json.loads(row['payload_json'])
                except (TypeError, ValueError):
                    continue
                payload['_event_time_ms'] = row['event_time_ms']
                rows.append(payload)
        else:
            with open(path, 'r', encoding='utf-8') as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        event_ms = int(row.get('event_time_ms', 0) or 0)
                        payload = dict(row.get('payload') or {})
                    except (TypeError, ValueError):
                        continue
                    if start_ms <= event_ms <= end_ms:
                        payload['_event_time_ms'] = event_ms
                        rows.append(payload)
    return sorted(rows, key=lambda item: item['_event_time_ms'])


def _files_for_window(root, stream, start_s, end_s):
    base = root / 'raw' / 'parquet' / stream
    cursor = int(start_s // 3600) * 3600
    paths = []
    while cursor <= end_s:
        dt = datetime.datetime.fromtimestamp(cursor, tz=datetime.timezone.utc)
        relative = Path(dt.strftime('%Y-%m-%d')) / dt.strftime('%H')
        parquet_path = base / relative.with_suffix('.parquet')
        wal_path = root / 'raw' / 'wal' / stream / relative.with_suffix('.jsonl')
        if parquet_path.exists():
            paths.append(parquet_path)
        elif wal_path.exists():
            paths.append(wal_path)
        cursor += 3600
    return paths


def _maker_fill(action, side, trades):
    quote = float(action['price'])
    queue = max(0.0, float(action.get('queue_ahead_qty', 0.0) or 0.0))
    at_quote = 0.0
    for trade in trades:
        price = float(trade.get('p', 0.0) or 0.0)
        qty = float(trade.get('q', 0.0) or 0.0)
        maker_is_buyer = bool(trade.get('m'))
        adverse_aggressor = maker_is_buyer if side == 'LONG' else not maker_is_buyer
        if side == 'LONG' and price < quote or side == 'SHORT' and price > quote:
            return True, trade['_event_time_ms'], 'TRADE_THROUGH'
        if abs(price - quote) <= 1e-9 and adverse_aggressor:
            at_quote += qty
            if queue > 0.0 and at_quote >= queue:
                return True, trade['_event_time_ms'], 'QUEUE_CONSUMED'
    return False, None, 'NO_CONSERVATIVE_FILL'


def _path_label(side, entry, features, fill_ms):
    sign = 1.0 if side == 'LONG' else -1.0
    prices = []
    for row in features:
        for name in ('mid_close', 'last_trade_price'):
            value = row.get(name)
            if value:
                prices.append((row['_event_time_ms'], float(value)))
                break
    moves = [
        (ts, sign * (price - entry) / entry * 10000.0)
        for ts, price in prices if ts >= fill_ms
    ]
    moves_15s = [(ts, move) for ts, move in moves if ts <= fill_ms + 15000]
    mfe = max((move for _, move in moves), default=0.0)
    mae = min((move for _, move in moves), default=0.0)
    def passage(sequence, favorable, adverse):
        for _, move in sequence:
            if move >= favorable:
                return 'FAVORABLE'
            if move <= -adverse:
                return 'ADVERSE'
        return 'NEITHER'
    pass_5_5 = passage(moves, 5.0, 5.0)
    pass_10_5 = passage(moves, 10.0, 5.0)
    pass_15_7 = passage(moves, 15.0, 7.0)
    toxic_2_4 = passage(moves_15s, 2.0, 4.0)
    good_5_4 = passage(moves_15s, 5.0, 4.0)
    return {
        'mfe_bps_180s': mfe, 'mae_bps_180s': mae,
        'plus5_before_minus5': pass_5_5 == 'FAVORABLE',
        'plus10_before_minus5': pass_10_5 == 'FAVORABLE',
        'plus15_before_minus7': pass_15_7 == 'FAVORABLE',
        'toxic_15s': toxic_2_4 == 'ADVERSE',
        'good_fill_15s': good_5_4 == 'FAVORABLE',
        'toxicity_outcome_15s': toxic_2_4,
    }


def label_file(states_path, recorder_root, output_path):
    groups = {}
    with open(states_path, 'r', encoding='utf-8') as source:
        for line in source:
            row = json.loads(line)
            payload = row.get('payload', row)
            if payload.get('event_type') != 'OPPORTUNITY_STATE':
                continue
            hour = int(float(payload['decision_time']) // 3600)
            groups.setdefault(hour, []).append(payload)
    labeled = 0
    with open(output_path, 'w', encoding='utf-8') as target:
        for payloads in groups.values():
            group_start = min(float(item['decision_time']) for item in payloads)
            group_end = max(float(item['decision_time']) for item in payloads) + 180.0
            trades_all = _payloads(
                _files_for_window(recorder_root, 'agg_trade', group_start, group_end),
                int(group_start * 1000), int(group_end * 1000),
            )
            features_all = _payloads(
                _files_for_window(recorder_root, 'feature_1s', group_start, group_end),
                int(group_start * 1000), int(group_end * 1000),
            )
            trade_times = [row['_event_time_ms'] for row in trades_all]
            feature_times = [row['_event_time_ms'] for row in features_all]
            for payload in payloads:
                decision_s = float(payload['decision_time'])
                start_ms, end_ms = int(decision_s * 1000), int((decision_s + 180.0) * 1000)
                trades = trades_all[
                    bisect.bisect_left(trade_times, start_ms):bisect.bisect_right(trade_times, end_ms)
                ]
                features = features_all[
                    bisect.bisect_left(feature_times, start_ms):bisect.bisect_right(feature_times, end_ms)
                ]
                # Never turn an incomplete future horizon into a negative label.
                if not features or features[-1]['_event_time_ms'] < end_ms - 1500:
                    continue
                for action in payload.get('action_candidates', ()):
                    if action.get('kind') == 'MARKET':
                        filled, fill_ms, basis = True, start_ms, 'MARKET'
                    else:
                        filled, fill_ms, basis = _maker_fill(action, payload['side'], trades)
                    record = {
                        'schema_version': 'ML_META_LABEL_V1',
                        'opportunity_id': payload.get('opportunity_id'),
                        'run_id': payload.get('run_id'), 'side': payload.get('side'),
                        'decision_time': decision_s,
                        'action': action, 'filled': filled, 'fill_time_ms': fill_ms,
                        'fill_basis': basis, 'feature_cutoff_ms': start_ms,
                        'label_window_end_ms': end_ms,
                        'causal_features': payload.get('causal_features'),
                        'path_label': _path_label(
                            payload['side'], float(action['price']), features, fill_ms
                        )
                        if filled else None,
                    }
                    target.write(json.dumps(record, separators=(',', ':')) + '\n')
                    labeled += 1
    return labeled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('states')
    parser.add_argument('--recorder', default='/home/ubuntu/smc2026_data')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    count = label_file(Path(args.states), Path(args.recorder), Path(args.output))
    print(json.dumps({'labeled_actions': count, 'output': args.output}))


if __name__ == '__main__':
    main()
