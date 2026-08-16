"""Strict structured-output contract for shadow research labels."""

import math


ANALYSIS_SCHEMA = {
    'type': 'object',
    'properties': {
        'market_regime': {
            'type': 'string',
            'enum': [
                'TREND_UP', 'TREND_DOWN', 'RANGE', 'TRANSITION',
                'VOLATILE', 'ILLIQUID', 'UNKNOWN',
            ],
        },
        'summary': {'type': 'string'},
        'failure_causes': {'type': 'array', 'items': {'type': 'string'}},
        'data_quality_flags': {'type': 'array', 'items': {'type': 'string'}},
        'recommendations': {'type': 'array', 'items': {'type': 'string'}},
        'supporting_evidence': {'type': 'array', 'items': {'type': 'string'}},
        'contradicting_evidence': {'type': 'array', 'items': {'type': 'string'}},
        'confidence': {'type': 'number', 'minimum': 0.0, 'maximum': 1.0},
    },
    'required': [
        'market_regime', 'summary', 'failure_causes', 'data_quality_flags',
        'recommendations', 'supporting_evidence', 'contradicting_evidence',
        'confidence',
    ],
    'additionalProperties': False,
}


def validate_analysis(value):
    if not isinstance(value, dict):
        raise ValueError('analysis must be an object')
    expected = set(ANALYSIS_SCHEMA['properties'])
    if set(value) != expected:
        raise ValueError('analysis fields do not match the schema')
    regimes = set(ANALYSIS_SCHEMA['properties']['market_regime']['enum'])
    if value['market_regime'] not in regimes:
        raise ValueError('invalid market_regime')
    if not isinstance(value['summary'], str) or not value['summary'].strip():
        raise ValueError('summary must be non-empty')
    for field in (
        'failure_causes', 'data_quality_flags', 'recommendations',
        'supporting_evidence', 'contradicting_evidence',
    ):
        rows = value[field]
        if not isinstance(rows, list) or any(not isinstance(item, str) for item in rows):
            raise ValueError(f'{field} must be a string array')
        value[field] = [item.strip()[:500] for item in rows[:20] if item.strip()]
    confidence = value['confidence']
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError('confidence must be numeric')
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError('confidence outside [0,1]')
    value['confidence'] = confidence
    value['summary'] = value['summary'].strip()[:2000]
    return value
