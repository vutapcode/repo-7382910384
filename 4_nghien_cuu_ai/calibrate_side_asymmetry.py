#!/usr/bin/env python3
"""Offline empirical-Bayes calibration; never imports or mutates live state."""

import argparse
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCORER_DIR = ROOT / '2_suy_luan_mapping' / 'tong_ket_chi_huy'
sys.path.insert(0, str(SCORER_DIR))
import side_calibration as schema  # noqa: E402


def _f(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _quantile(values, q):
    ordered = sorted(_f(value) for value in values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * q
    lo, hi = int(index), min(len(ordered) - 1, int(index) + 1)
    part = index - lo
    return ordered[lo] * (1.0 - part) + ordered[hi] * part


def _load(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _causal_snapshots(events_path):
    result = {}
    try:
        handle = Path(events_path).open('r', encoding='utf-8')
    except OSError:
        return result
    with handle:
        for line in handle:
            if 'CONTINUOUS_V2_1_SIDE' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get('payload', {}) or {}
            if event.get('event') != 'CONTINUOUS_SCORE_SHADOW':
                continue
            if payload.get('version') != 'CONTINUOUS_V2_1_SIDE':
                continue
            breakdown = payload.get('breakdown', {}) or {}
            opportunity_id = payload.get('opportunity_id')
            decision_time = _f(breakdown.get('snapshot_time'), event.get('ts'))
            if not opportunity_id or not breakdown.get('causal_components'):
                continue
            if any(
                _f(row.get('event_time_max')) > decision_time + 1e-6
                for row in breakdown.get('causal_components', ())
                if isinstance(row, dict) and row.get('event_time_max') is not None
            ):
                continue
            previous = result.get(opportunity_id)
            if previous is None or decision_time < previous['decision_time']:
                result[opportunity_id] = {
                    'decision_time': decision_time,
                    'breakdown': breakdown,
                    'side': payload.get('side') or breakdown.get('selected_bias'),
                    'mode': payload.get('mode') or breakdown.get('mode'),
                }
    return result


def _samples(snapshot, causal):
    registry = snapshot.get('side_calibration_shadow_registry', {}) or {}
    cycles = snapshot.get('cycles', ()) or ()
    cycle_labels = {}
    for cycle in cycles:
        opportunity_id = cycle.get('opportunity_id')
        strategy = cycle.get('strategy_mainnet', {}) or {}
        attribution = cycle.get('venue_attribution', {}) or {}
        if not opportunity_id or attribution.get('exclude_from_calibration'):
            continue
        if not strategy.get('valid_for_calibration', False):
            continue
        if strategy.get('net_pnl_bps') is None:
            continue
        cycle_labels[opportunity_id] = {
            'net_bps': _f(strategy.get('net_pnl_bps')),
            'holding_ms': _f(cycle.get('holding_time_ms')),
        }
    rows = []
    for opportunity_id, record in registry.items():
        first = causal.get(opportunity_id)
        terminal = record.get('terminal', {}) or {}
        followup = terminal.get('followup', {}) or {}
        if first is None or not terminal or not followup.get('completed'):
            continue
        side = str(record.get('side') or first.get('side') or '').upper()
        if side not in ('LONG', 'SHORT'):
            continue
        mode = record.get('mode') or first.get('mode')
        label = cycle_labels.get(opportunity_id)
        rows.append({
            'opportunity_id': opportunity_id, 'side': side,
            'archetype': schema.archetype_for(mode),
            'regime': schema.regime_for(
                mode, first['breakdown'].get('trend_m15')
            ),
            'decision_time': first['decision_time'],
            'mfe_bps': _f(followup.get('mfe_bps')),
            'mae_bps': _f(followup.get('mae_bps')),
            'net_bps': None if label is None else label['net_bps'],
            'time_to_target_ms': None if label is None else label['holding_ms'],
            'snapshot_causal_complete': True,
            'source_graph_valid': bool(first['breakdown'].get('causal_components')),
            'breakdown': first['breakdown'],
        })
    return rows


def _cell(rows, side, archetype, regime, parent_mean=0.0, prior_strength=20.0):
    chosen = [
        row for row in rows
        if (side == 'GLOBAL' or row['side'] == side)
        and (archetype == 'ALL' or row['archetype'] == archetype)
        and (regime == 'ALL' or row['regime'] == regime)
    ]
    labeled = [row for row in chosen if row['net_bps'] is not None]
    n = len(chosen)
    labeled_n = len(labeled)
    shrinkage = prior_strength / (prior_strength + labeled_n)
    sample_mean = statistics.fmean(row['net_bps'] for row in labeled) if labeled else parent_mean
    posterior_mean = shrinkage * parent_mean + (1.0 - shrinkage) * sample_mean
    if labeled_n >= 2:
        sem = statistics.stdev(row['net_bps'] for row in labeled) / math.sqrt(labeled_n)
    else:
        sem = 99.0
    lcb = posterior_mean - 1.645 * sem
    fee_clear = (
        sum(row['net_bps'] >= 2.0 for row in labeled) / labeled_n
        if labeled_n else 0.5
    )
    mae_p95 = _quantile([abs(row['mae_bps']) for row in chosen], 0.95)
    evidence_multiplier = max(0.85, min(1.15, 1.0 + posterior_mean / 100.0))
    side_sign = 1.0 if side in ('LONG', 'GLOBAL') else -1.0

    def reliability(predictions):
        usable = [(pred, outcome) for pred, outcome in predictions if abs(pred) > 0.02]
        correct = sum((pred > 0.0) == (outcome > 0.0) for pred, outcome in usable)
        probability = (correct + 10.0) / (len(usable) + 20.0)
        return max(0.85, min(1.15, 0.85 + 0.30 * probability))

    momentum_reliability = {}
    for horizon in (15, 60, 180):
        predictions = []
        for row in labeled:
            selected_side_sign = 1.0 if row['side'] == 'LONG' else -1.0
            horizon_row = (
                row['breakdown'].get('momentum_breakdown', {}).get('horizons', {})
                .get(str(horizon), {})
            )
            predictions.append((
                selected_side_sign * _f(horizon_row.get('price_normalized')),
                row['net_bps'],
            ))
        momentum_reliability[str(horizon)] = reliability(predictions)
    family_predictions = defaultdict(list)
    for row in labeled:
        for component in row['breakdown'].get('causal_components', ()):
            effect = _f(component.get('effect'))
            for member in component.get('members', ()):
                if member != 'M15_STRUCTURE':
                    family_predictions[member].append((effect, row['net_bps']))
    reversal_reliability = {
        family: reliability(predictions)
        for family, predictions in family_predictions.items()
    }
    flags = []
    if n < 40:
        flags.append('CELL_SAMPLE_LT_40')
    if labeled_n < 40:
        flags.append('LABELED_SAMPLE_LT_40')
    if lcb <= 0.0:
        flags.append('NET_EDGE_LCB_NON_POSITIVE')
    # OOS comparator is deliberately mandatory; calibration data cannot approve itself.
    flags.append('OOS_NOT_IMPROVED')
    return {
        'side': side, 'archetype': archetype, 'regime': regime,
        'source_level': (
            'GLOBAL' if side == 'GLOBAL' else 'SIDE' if archetype == 'ALL'
            else 'SIDE_ARCHETYPE' if regime == 'ALL' else 'CELL'
        ),
        'effective_sample_size': n,
        'labeled_sample_size': labeled_n,
        'momentum_reliability': momentum_reliability,
        'reversal_family_reliability': reversal_reliability,
        'poc_weight': 4.0, 'noise_scale': max(0.5, min(2.0, mae_p95 / 8.0 or 1.0)),
        'fee_clear_probability': fee_clear,
        'expected_net_bps': posterior_mean, 'net_edge_lcb': lcb,
        'parent_shrinkage': shrinkage,
        'evidence_multiplier': evidence_multiplier,
        'confidence_multiplier': max(0.85, min(1.15, 0.9 + 0.2 * fee_clear)),
        'activation_multiplier': 1.0, 'floor_multiplier': 1.0,
        'size_multiplier': evidence_multiplier,
        'mae_p95_bps': mae_p95,
        'bounded_live_eligible': False,
        'quality_flags': flags,
    }


def build(snapshot_path, events_path):
    snapshot = _load(snapshot_path)
    causal = _causal_snapshots(events_path)
    rows = _samples(snapshot, causal)
    global_cell = _cell(rows, 'GLOBAL', 'ALL', 'ALL')
    cells = [global_cell]
    global_mean = global_cell['expected_net_bps']
    for side in ('LONG', 'SHORT'):
        side_cell = _cell(rows, side, 'ALL', 'ALL', global_mean, 30.0)
        cells.append(side_cell)
        for archetype in ('REVERSAL', 'PULLBACK', 'BREAKOUT', 'NEUTRAL_MOMENTUM'):
            archetype_cell = _cell(
                rows, side, archetype, 'ALL', side_cell['expected_net_bps'], 20.0
            )
            cells.append(archetype_cell)
            for regime in ('TREND', 'TRANSITION', 'RANGE_NEUTRAL'):
                cells.append(_cell(
                    rows, side, archetype, regime,
                    archetype_cell['expected_net_bps'], 15.0,
                ))
    side_counts = {side: sum(row['side'] == side for row in rows) for side in ('LONG', 'SHORT')}
    blocks = {time.strftime('%Y-%m-%d', time.gmtime(row['decision_time'])) for row in rows}
    artifact = {
        'schema_version': schema.SCHEMA_VERSION,
        'calibration_version': 'SIDE_EB_' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()),
        'trained_until': max((row['decision_time'] for row in rows), default=None),
        'global_opportunity_count': len(rows),
        'side_opportunity_counts': side_counts,
        'evaluation_side_counts': dict(side_counts),
        'evaluation_time_blocks': len(blocks),
        'cells': cells,
        'quality_flags': ['SHADOW_ONLY', 'OOS_APPROVAL_REQUIRED'],
        'training_contract': {
            'one_opportunity_one_sample': True,
            'testnet_direction_labels_used': False,
            'future_mfe_mae_used_as_features': False,
        },
    }
    artifact['artifact_hash'] = schema.canonical_hash(artifact)
    return artifact


def main():
    parser = argparse.ArgumentParser()
    journal = ROOT / '3_thuc_thi' / 'quan_ly_vi_the' / 'nhat_ky'
    parser.add_argument('--snapshot', default=str(journal / 'cycles.json'))
    parser.add_argument('--events', default=str(journal / 'events.jsonl'))
    parser.add_argument('--output-dir', default=str(ROOT / 'derived' / 'side_calibration'))
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    artifact = build(args.snapshot, args.events)
    if args.write:
        root = Path(args.output_dir)
        root.mkdir(parents=True, exist_ok=True)
        path = root / (artifact['calibration_version'] + '.json')
        if path.exists():
            raise SystemExit('append-only artifact already exists: ' + str(path))
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(path)
    else:
        print(json.dumps(artifact, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
