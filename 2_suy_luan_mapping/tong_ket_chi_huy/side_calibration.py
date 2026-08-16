"""Versioned, read-only LONG/SHORT calibration artifacts for V2.1."""

import hashlib
import json
import math
import os
import time
from pathlib import Path


SCHEMA_VERSION = 1
CALIBRATION_VERSION = 'SIDE_CALIBRATION_PRIOR_V1'
DEFAULT_DIR = Path(__file__).resolve().parents[2] / 'derived' / 'side_calibration'
_CACHE = {'loaded_at': 0.0, 'directory': None, 'artifact': None}


def _finite(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _clamp(value, low, high):
    return max(low, min(high, _finite(value)))


def canonical_hash(artifact):
    clean = dict(artifact or {})
    clean.pop('artifact_hash', None)
    encoded = json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def prior_artifact():
    artifact = {
        'schema_version': SCHEMA_VERSION,
        'calibration_version': CALIBRATION_VERSION,
        'trained_until': None,
        'global_opportunity_count': 0,
        'side_opportunity_counts': {'LONG': 0, 'SHORT': 0},
        'evaluation_side_counts': {'LONG': 0, 'SHORT': 0},
        'evaluation_time_blocks': 0,
        'cells': [],
        'quality_flags': ['PRIOR_ONLY', 'INSUFFICIENT_SAMPLE'],
    }
    artifact['artifact_hash'] = canonical_hash(artifact)
    return artifact


def _valid_artifact(data):
    if not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION:
        return False
    if not isinstance(data.get('cells'), list):
        return False
    return data.get('artifact_hash') == canonical_hash(data)


def load_latest(directory=None):
    root = Path(directory or os.getenv('SMC_SIDE_CALIBRATION_DIR') or DEFAULT_DIR)
    now = time.monotonic()
    if (
        _CACHE['artifact'] is not None and _CACHE['directory'] == str(root)
        and now - _CACHE['loaded_at'] < 5.0
    ):
        return dict(_CACHE['artifact'])
    for path in sorted(root.glob('*.json'), reverse=True):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if _valid_artifact(data):
            data = dict(data)
            data['_artifact_path'] = str(path)
            _CACHE.update({
                'loaded_at': now, 'directory': str(root), 'artifact': dict(data),
            })
            return data
    data = prior_artifact()
    _CACHE.update({
        'loaded_at': now, 'directory': str(root), 'artifact': dict(data),
    })
    return data


def archetype_for(mode):
    mode = str(mode or '').upper()
    if 'NEUTRAL-MOMENTUM' in mode or 'NEUTRAL_MOMENTUM' in mode:
        return 'NEUTRAL_MOMENTUM'
    if 'BREAKOUT' in mode:
        return 'BREAKOUT'
    if 'PULLBACK' in mode:
        return 'PULLBACK'
    return 'REVERSAL'


def regime_for(mode, trend_m15=None):
    mode = str(mode or '').upper()
    trend = str(trend_m15 or '').upper()
    if 'TRANSITION' in mode:
        return 'TRANSITION'
    if 'TREND' in mode or trend in ('BULLISH', 'BEARISH'):
        return 'TREND'
    return 'RANGE_NEUTRAL'


def _cell_key(cell):
    return (
        str(cell.get('side') or '').upper(),
        str(cell.get('archetype') or '').upper(),
        str(cell.get('regime') or '').upper(),
    )


def profile_for(artifact, side, archetype, regime):
    side, archetype, regime = side.upper(), archetype.upper(), regime.upper()
    cells = {
        _cell_key(cell): cell for cell in artifact.get('cells', ())
        if isinstance(cell, dict)
    }
    candidates = (
        (side, archetype, regime),
        (side, archetype, 'ALL'),
        (side, 'ALL', 'ALL'),
        ('GLOBAL', 'ALL', 'ALL'),
    )
    selected = next((cells[key] for key in candidates if key in cells), {})
    profile = {
        'side': side, 'archetype': archetype, 'regime': regime,
        'effective_sample_size': _finite(selected.get('effective_sample_size')),
        'momentum_reliability': dict(selected.get('momentum_reliability', {}) or {}),
        'reversal_family_reliability': dict(
            selected.get('reversal_family_reliability', {}) or {}
        ),
        'poc_weight': _clamp(selected.get('poc_weight', 4.0), 0.0, 8.0),
        'noise_scale': _clamp(selected.get('noise_scale', 1.0), 0.50, 2.0),
        'fee_clear_probability': _clamp(
            selected.get('fee_clear_probability', 0.5), 0.0, 1.0
        ),
        'expected_net_bps': _finite(selected.get('expected_net_bps')),
        'net_edge_lcb': _finite(selected.get('net_edge_lcb')),
        'parent_shrinkage': _clamp(selected.get('parent_shrinkage', 1.0), 0.0, 1.0),
        'evidence_multiplier': _clamp(
            selected.get('evidence_multiplier', 1.0), 0.85, 1.15
        ),
        'confidence_multiplier': _clamp(
            selected.get('confidence_multiplier', 1.0), 0.85, 1.15
        ),
        'activation_multiplier': _clamp(
            selected.get('activation_multiplier', 1.0), 0.85, 1.15
        ),
        'floor_multiplier': _clamp(
            selected.get('floor_multiplier', 1.0), 0.90, 1.10
        ),
        'size_multiplier': _clamp(
            selected.get('size_multiplier', 1.0), 0.85, 1.15
        ),
        'bounded_live_eligible': bool(selected.get('bounded_live_eligible')),
        'quality_flags': list(selected.get('quality_flags', ()) or ()),
        'source_level': selected.get('source_level', 'PRIOR'),
    }
    return profile


def bounded_live_allowed(artifact, profile):
    counts = artifact.get('side_opportunity_counts', {}) or {}
    eval_counts = artifact.get('evaluation_side_counts', {}) or {}
    side = profile['side']
    return bool(
        profile.get('bounded_live_eligible')
        and profile['effective_sample_size'] >= 40
        and _finite(counts.get(side)) >= 100
        and _finite(artifact.get('global_opportunity_count')) >= 200
        and _finite(eval_counts.get('LONG')) >= 75
        and _finite(eval_counts.get('SHORT')) >= 75
        and _finite(artifact.get('evaluation_time_blocks')) >= 3
        and profile['net_edge_lcb'] > 0.0
        and 'OOS_NOT_IMPROVED' not in profile['quality_flags']
        and 'TAIL_MAE_P95_REGRESSION' not in profile['quality_flags']
    )
