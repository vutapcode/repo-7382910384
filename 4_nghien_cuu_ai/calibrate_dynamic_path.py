#!/usr/bin/env python3
"""Offline, versioned calibration for Dynamic Path V2.

This script never touches live state.  It refuses to emit a model until the
locked independence/class-balance/mode-coverage gates are satisfied.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path


MIN_OPPORTUNITIES = 300
MIN_HITS = 75
MIN_MISSES = 75
MIN_PER_MODE = 30
FEATURES = (
    'distance_atr', 'obstacle_count', 'support', 'flow_response',
    'data_confidence', 'adverse', 'price_progress_atr',
)


def _f(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _sigmoid(value):
    if value >= 0:
        z = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -60.0))
    return z / (1.0 + z)


def _rows(snapshot):
    cycles = snapshot.get('cycles', ()) if isinstance(snapshot, dict) else ()
    rows = []
    opportunities = set()
    modes = {}
    for cycle in cycles:
        if not isinstance(cycle, dict):
            continue
        attribution = cycle.get('venue_attribution', {}) or {}
        strategy = cycle.get('strategy_mainnet', {}) or {}
        if attribution.get('exclude_from_calibration'):
            continue
        if not strategy.get('valid_for_calibration', False):
            continue
        plan = cycle.get('dynamic_exit_plan', {}) or {}
        labels = cycle.get('path_calibration_label', {}) or {}
        if plan.get('version') != 'DYNAMIC_PATH_V2':
            continue
        opportunity = str(
            cycle.get('opportunity_id') or labels.get('opportunity_id') or ''
        )
        if not opportunity or opportunity in opportunities:
            continue
        opportunities.add(opportunity)
        mode = str(cycle.get('mode') or 'UNKNOWN')
        modes[mode] = modes.get(mode, 0) + 1
        label_by_id = {
            item.get('target_id'): bool(item.get('hit_before_terminal'))
            for item in labels.get('targets', ()) if isinstance(item, dict)
        }
        for candidate in plan.get('target_candidates', ()):
            target_id = candidate.get('target_id')
            if target_id not in label_by_id:
                continue
            context = candidate.get('context', {}) or {}
            feature = {
                'distance_atr': _f(candidate.get('distance_atr')),
                'obstacle_count': _f(candidate.get('obstacle_count')),
                'support': _f(context.get('support')),
                'flow_response': _f(context.get('flow_response')),
                'data_confidence': _f(context.get('data_confidence')),
                'adverse': _f(context.get('adverse')),
                'price_progress_atr': _f(context.get('price_progress_atr')),
            }
            rows.append({
                'created_at': _f(cycle.get('created_at')),
                'opportunity_id': opportunity, 'mode': mode,
                'x': feature, 'y': 1 if label_by_id[target_id] else 0,
                'prior_p': _f(candidate.get('p_hit_before_stop'), 0.5),
            })
    rows.sort(key=lambda row: row['created_at'])
    return rows, opportunities, modes


def _readiness(rows, opportunities, modes):
    hits = sum(row['y'] for row in rows)
    misses = len(rows) - hits
    failures = []
    if len(opportunities) < MIN_OPPORTUNITIES:
        failures.append('INDEPENDENT_OPPORTUNITIES_BELOW_300')
    if hits < MIN_HITS:
        failures.append('TARGET_HITS_BELOW_75')
    if misses < MIN_MISSES:
        failures.append('TARGET_MISSES_BELOW_75')
    if any(count < MIN_PER_MODE for count in modes.values()):
        failures.append('MODE_COVERAGE_BELOW_30')
    return {
        'ready': not failures, 'failures': failures,
        'opportunities': len(opportunities), 'rows': len(rows),
        'hits': hits, 'misses': misses, 'mode_counts': modes,
    }


def _normalizer(rows):
    means, scales = {}, {}
    for name in FEATURES:
        values = [row['x'][name] for row in rows]
        mean = sum(values) / max(1, len(values))
        variance = sum((value - mean) ** 2 for value in values) / max(1, len(values))
        means[name] = mean
        scales[name] = max(math.sqrt(variance), 1e-6)
    return means, scales


def _vector(row, means, scales):
    return [1.0] + [
        (row['x'][name] - means[name]) / scales[name] for name in FEATURES
    ]


def _fit(rows, means, scales, epochs=1200, rate=0.04, l2=0.02):
    weights = [0.0] * (len(FEATURES) + 1)
    for _ in range(epochs):
        gradient = [0.0] * len(weights)
        for row in rows:
            vector = _vector(row, means, scales)
            error = _sigmoid(sum(w * x for w, x in zip(weights, vector))) - row['y']
            for index, value in enumerate(vector):
                gradient[index] += error * value
        count = max(1, len(rows))
        for index in range(len(weights)):
            penalty = 0.0 if index == 0 else l2 * weights[index]
            weights[index] -= rate * (gradient[index] / count + penalty)
    return weights


def _predict(row, weights, means, scales):
    vector = _vector(row, means, scales)
    return _sigmoid(sum(w * x for w, x in zip(weights, vector)))


def _metrics(rows, probabilities):
    if not rows:
        return {'brier': None, 'log_loss': None, 'ece': None}
    eps = 1e-9
    brier = sum((p - row['y']) ** 2 for p, row in zip(probabilities, rows)) / len(rows)
    log_loss = -sum(
        row['y'] * math.log(max(eps, p))
        + (1 - row['y']) * math.log(max(eps, 1.0 - p))
        for p, row in zip(probabilities, rows)
    ) / len(rows)
    ece = 0.0
    for low in (index / 10.0 for index in range(10)):
        bucket = [
            (p, row['y']) for p, row in zip(probabilities, rows)
            if low <= p < low + 0.1 or (low == 0.9 and p == 1.0)
        ]
        if bucket:
            ece += len(bucket) / len(rows) * abs(
                sum(p for p, _ in bucket) / len(bucket)
                - sum(y for _, y in bucket) / len(bucket)
            )
    return {'brier': brier, 'log_loss': log_loss, 'ece': ece}


def calibrate(snapshot):
    rows, opportunities, modes = _rows(snapshot)
    readiness = _readiness(rows, opportunities, modes)
    result = {'readiness': readiness, 'promoted': False}
    if not readiness['ready']:
        return result
    cut1 = max(1, int(len(rows) * 0.60))
    cut2 = max(cut1 + 1, int(len(rows) * 0.80))
    train, calibration, test = rows[:cut1], rows[cut1:cut2], rows[cut2:]
    means, scales = _normalizer(train)
    weights = _fit(train, means, scales)
    # Platt-scale the model score on the chronological calibration slice.
    raw_cal = [{**row, 'x': {'raw': _predict(row, weights, means, scales)}} for row in calibration]
    platt_means = {'raw': sum(row['x']['raw'] for row in raw_cal) / len(raw_cal)}
    variance = sum((row['x']['raw'] - platt_means['raw']) ** 2 for row in raw_cal) / len(raw_cal)
    platt_scales = {'raw': max(math.sqrt(variance), 1e-6)}
    original_features = globals()['FEATURES']
    globals()['FEATURES'] = ('raw',)
    try:
        platt = _fit(raw_cal, platt_means, platt_scales, epochs=600, rate=0.03, l2=0.01)
        raw_test = [_predict(row, weights, means, scales) for row in test]
        calibrated = [
            _sigmoid(platt[0] + platt[1] * ((p - platt_means['raw']) / platt_scales['raw']))
            for p in raw_test
        ]
    finally:
        globals()['FEATURES'] = original_features
    learned = _metrics(test, calibrated)
    prior = _metrics(test, [row['prior_p'] for row in test])
    promoted = bool(
        learned['ece'] <= 0.08
        and learned['brier'] < prior['brier']
        and learned['log_loss'] < prior['log_loss']
    )
    model = {
        'model_version': 'PATH_LOGISTIC_CALIBRATED_V1',
        'feature_schema': 'PATH_FEATURES_V1',
        'features': list(FEATURES), 'weights': weights,
        'means': means, 'scales': scales,
        'platt_weights': platt, 'platt_mean': platt_means['raw'],
        'platt_scale': platt_scales['raw'],
        'chronological_split': {'train': len(train), 'calibration': len(calibration), 'test': len(test)},
        'metrics': {'learned': learned, 'prior': prior},
        'promotion_pass': promoted,
    }
    canonical = json.dumps(model, sort_keys=True, separators=(',', ':')).encode()
    model['checksum_sha256'] = hashlib.sha256(canonical).hexdigest()
    result.update({'promoted': promoted, 'model': model})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('cycles_json', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = calibrate(json.loads(args.cycles_json.read_text(encoding='utf-8')))
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output and result.get('promoted'):
        args.output.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)


if __name__ == '__main__':
    main()
