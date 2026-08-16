"""Versioned ML artifact validation and bounded authority gates."""

import hashlib
import json
import os
from pathlib import Path


MODES = ('OFF', 'SHADOW', 'QUOTE_ONLY', 'SCOUT_MAKER', 'BOUNDED_ACTION')
REQUIRED_COUNTS = {
    'QUOTE_ONLY': {'opportunity_states': 800, 'days': 7, 'ladder_actions_balanced': 1},
    'SCOUT_MAKER': {
        'opportunity_states': 800, 'days': 7, 'ladder_actions_balanced': 1,
        'counterfactual_fills': 500, 'good_fills': 100, 'toxic_fills': 100,
    },
    'BOUNDED_ACTION': {
        'opportunities': 2000, 'days': 14, 'time_blocks': 5,
        'long_opportunities': 500, 'short_opportunities': 500,
    },
}


def promotion_gate(payload, requested):
    """Every right needs data volume and out-of-sample quality, not a flag alone."""
    counts = dict(payload.get('counts', {}) or {})
    metrics = dict(payload.get('out_of_sample_metrics', {}) or {})
    needed = REQUIRED_COUNTS.get(requested, {})
    reasons = [
        f'COUNT_{name}' for name, minimum in needed.items()
        if float(counts.get(name, 0) or 0) < float(minimum)
    ]
    def metric(name, fallback):
        value = metrics.get(name)
        return float(fallback if value is None else value)
    checks = (
        ('ECE', metric('ece', 1.0) <= 0.08),
        ('BRIER', metric('brier', 1.0) < metric('prior_brier', 0.0)),
        ('LOGLOSS', metric('logloss', 99.0) < metric('baseline_logloss', 0.0)),
        ('NET_EDGE_LCB', metric('net_edge_lcb_bps', -1.0) > 0.0),
        ('RECALL', metric('profitable_recall_delta', -1.0) >= -0.05),
        ('JUNK', metric('junk_action_reduction', 0.0) >= 0.15),
        ('TAIL_MAE', metric('tail_mae_ratio', 99.0) <= 1.10),
        ('CHURN', metric('cancel_reprice_delta', 1.0) <= 0.0),
        ('CPU_BOT', metric('bot_cpu_pct', 100.0) < 20.0),
        ('CPU_RECORDER', metric('recorder_cpu_pct', 100.0) < 20.0),
    )
    reasons.extend(name for name, passed in checks if not passed)
    if int(metrics.get('walk_forward_blocks', 0) or 0) < 5:
        reasons.append('WALK_FORWARD_BLOCKS')
    return not reasons, reasons


def canonical_hash(payload):
    clean = dict(payload)
    clean.pop('artifact_hash', None)
    return hashlib.sha256(json.dumps(
        clean, sort_keys=True, separators=(',', ':'),
    ).encode()).hexdigest()


def requested_mode():
    mode = str(os.getenv('SMC_ML_META_MODE', 'SHADOW')).upper()
    return mode if mode in MODES else 'OFF'


def load_artifact(path=None):
    target = Path(path or os.getenv(
        'SMC_ML_META_ARTIFACT',
        '/home/ubuntu/SMC2026/derived/ml_meta/models/active.json',
    ))
    try:
        payload = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, 'ARTIFACT_MISSING_OR_INVALID'
    if payload.get('artifact_hash') != canonical_hash(payload):
        return None, 'ARTIFACT_HASH_MISMATCH'
    if payload.get('feature_schema') != 'ML_META_FEATURES_V1':
        return None, 'FEATURE_SCHEMA_MISMATCH'
    return payload, 'VALID'


def authority(mode, artifact):
    if mode in ('OFF', 'SHADOW'):
        return {'mode': mode, 'quote': False, 'scout_order': False, 'market': False}
    if not artifact or not artifact.get('promotion_pass'):
        return {'mode': 'SHADOW', 'quote': False, 'scout_order': False, 'market': False}
    approved = str(artifact.get('approved_mode', 'SHADOW')).upper()
    if approved not in MODES:
        return {'mode': 'SHADOW', 'quote': False, 'scout_order': False, 'market': False}
    gate_passed, gate_reasons = promotion_gate(artifact, approved)
    if not gate_passed:
        return {
            'mode': 'SHADOW', 'quote': False, 'scout_order': False,
            'market': False, 'gate_reasons': gate_reasons,
        }
    requested_rank, approved_rank = MODES.index(mode), MODES.index(approved)
    effective = MODES[min(requested_rank, approved_rank)]
    return {
        'mode': effective,
        'quote': effective in ('QUOTE_ONLY', 'SCOUT_MAKER', 'BOUNDED_ACTION'),
        'scout_order': effective in ('SCOUT_MAKER', 'BOUNDED_ACTION'),
        'market': effective == 'BOUNDED_ACTION',
    }
