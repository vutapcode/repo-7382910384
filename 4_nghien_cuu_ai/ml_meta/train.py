"""Offline, one-thread staged trainer. Never promotes or edits active.json."""

import argparse
import hashlib
import json
import math
import time
from collections import Counter
from pathlib import Path

import numpy as np


SEEDS = (17, 43, 101)
FEATURE_SCHEMA = 'ML_META_FEATURES_V1'


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _flatten(row):
    features = row.get('causal_features') or {}
    action = row.get('action') or {}
    values = []
    names = sorted(features)
    for name in names:
        value = features.get(name)
        values.append(float(value) if isinstance(value, (int, float)) and math.isfinite(value) else np.nan)
    values.extend([
        float(action.get('price', np.nan)),
        float(action.get('distance_from_bbo_ticks', np.nan)),
        float(action.get('queue_ahead_qty', np.nan)),
        1.0 if action.get('kind') == 'MARKET' else 0.0,
        1.0 if row.get('side') == 'LONG' else -1.0,
    ])
    return names + ['action_price', 'distance_ticks', 'queue_ahead_qty', 'is_market', 'side_sign'], values


def load_rows(path):
    rows = []
    with open(path, 'r', encoding='utf-8') as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get('run_id') and row.get('opportunity_id') and row.get('causal_features'):
                rows.append(row)
    return rows


def readiness(rows):
    opportunities = {(row.get('run_id'), row.get('opportunity_id')) for row in rows}
    opportunity_states = {
        (row.get('run_id'), row.get('opportunity_id'), row.get('feature_cutoff_ms'))
        for row in rows
    }
    days = {int(float(row.get('feature_cutoff_ms', 0)) // 86400000) for row in rows}
    fills = [
        row for row in rows
        if row.get('filled') and (row.get('action') or {}).get('kind') == 'MAKER'
    ]
    good = sum(bool((row.get('path_label') or {}).get('good_fill_15s')) for row in fills)
    toxic = sum(bool((row.get('path_label') or {}).get('toxic_15s')) for row in fills)
    actions = Counter((row.get('action') or {}).get('action_id') for row in rows)
    passive_outcomes = {}
    for row in rows:
        action = row.get('action') or {}
        if action.get('kind') != 'MAKER':
            continue
        counts = passive_outcomes.setdefault(action.get('action_id'), {'positive': 0, 'negative': 0})
        counts['positive' if row.get('filled') else 'negative'] += 1
    ladder_covered = bool(passive_outcomes) and all(
        counts['positive'] >= 20 and counts['negative'] >= 20
        for counts in passive_outcomes.values()
    )
    return {
        'opportunities': len(opportunities),
        'opportunity_states': len(opportunity_states),
        'days': len(days), 'counterfactual_fills': len(fills),
        'good_fills': good, 'toxic_fills': toxic,
        'action_counts': dict(actions), 'passive_action_outcomes': passive_outcomes,
        'fill_ready': len(opportunity_states) >= 800 and len(days) >= 7 and ladder_covered,
        'toxicity_ready': len(fills) >= 500 and good >= 100 and toxic >= 100,
        'move_path_ready': len(opportunities) >= 1500 and len(days) >= 14,
        'meta_ready': len(opportunities) >= 2000 and len(days) >= 14,
    }


def train_binary(rows, label_getter, target, output):
    import xgboost as xgb
    names, _ = _flatten(rows[0])
    matrix = np.asarray([_flatten(row)[1] for row in rows], dtype=float)
    labels = np.asarray([float(label_getter(row)) for row in rows])
    order = np.argsort([float(row.get('feature_cutoff_ms', 0)) for row in rows])
    split = max(1, int(len(order) * 0.8))
    train_idx, valid_idx = order[:split], order[split:]
    if len(valid_idx) == 0 or len(set(labels[train_idx])) < 2:
        raise ValueError('chronological split lacks both classes')
    model_files = []
    predictions = []
    for seed in SEEDS:
        booster = xgb.train({
            'objective': 'binary:logistic', 'eval_metric': 'logloss',
            'max_depth': 4, 'eta': 0.04, 'subsample': 0.80,
            'colsample_bytree': 0.80, 'seed': seed, 'nthread': 1,
        }, xgb.DMatrix(matrix[train_idx], label=labels[train_idx], feature_names=names),
            num_boost_round=250, verbose_eval=False)
        path = output / f'{target}_seed_{seed}.ubj'
        booster.save_model(path)
        model_files.append({'path': path.name, 'sha256': _hash(path), 'seed': seed})
        predictions.append(booster.predict(xgb.DMatrix(matrix[valid_idx], feature_names=names)))
    mean = np.mean(predictions, axis=0)
    truth = labels[valid_idx]
    brier = float(np.mean((mean - truth) ** 2))
    eps = 1e-7
    logloss = float(-np.mean(truth * np.log(mean + eps) + (1 - truth) * np.log(1 - mean + eps)))
    uncertainty = np.std(predictions, axis=0)
    return {
        'target': target, 'models': model_files, 'feature_names': names,
        'validation': {'brier': brier, 'logloss': logloss,
                       'ensemble_uncertainty_q90': float(np.quantile(uncertainty, 0.90)),
                       'samples': len(valid_idx)},
        'calibration': 'PENDING_ECE_AND_CONFORMAL_GATE',
    }


def run(labels, output):
    rows = load_rows(labels)
    status = readiness(rows)
    output.mkdir(parents=True, exist_ok=True)
    report = {
        'schema_version': 'ML_META_TRAIN_REPORT_V1', 'created_at': time.time(),
        'feature_schema': FEATURE_SCHEMA, 'labels_sha256': _hash(labels),
        'readiness': status, 'models': [], 'promotion_pass': False,
        'approved_mode': 'SHADOW', 'reason': 'INSUFFICIENT_DATA',
    }
    if status['fill_ready']:
        maker_rows = [
            row for row in rows if (row.get('action') or {}).get('kind') == 'MAKER'
        ]
        report['models'].append(train_binary(
            maker_rows, lambda row: row.get('filled'), 'fill', output
        ))
        report['reason'] = 'MODELS_TRAINED_AWAITING_CALIBRATION_PROMOTION_GATES'
    if status['toxicity_ready']:
        filled = [row for row in rows if row.get('filled') and row.get('path_label')]
        report['models'].append(train_binary(
            filled, lambda row: (row.get('path_label') or {}).get('toxic_15s'),
            'toxicity', output,
        ))
    report_path = output / 'train_report.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('labels', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.labels, args.output)))


if __name__ == '__main__':
    main()
