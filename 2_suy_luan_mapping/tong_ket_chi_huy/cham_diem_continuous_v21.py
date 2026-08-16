"""CONTINUOUS V2.1 side calibration. Shadow by default; never mutates state."""

import importlib.util
import math
import os
from pathlib import Path


CURRENT_DIR = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v2 = _load('continuous_v21_base', CURRENT_DIR / 'cham_diem_continuous_v2.py')
calibration = _load('continuous_v21_calibration', CURRENT_DIR / 'side_calibration.py')
VERSION = 'CONTINUOUS_V2_1_SIDE'


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def _mode():
    value = str(os.getenv('SMC_SIDE_CALIBRATION_MODE', 'SHADOW')).upper()
    return value if value in ('SHADOW', 'BOUNDED_LIVE', 'SYMMETRIC_V2') else 'SHADOW'


def _poc_relevance(snapshot, archetype, selected, momentum):
    now = float(getattr(snapshot, 'snapshot_time', 0.0) or 0.0)
    updated = float(getattr(snapshot, 'volume_profile_updated_at', 0.0) or 0.0)
    age = max(0.0, now - updated) if updated > 0.0 else 60.0
    freshness = _clamp(1.0 - age / 600.0)
    coverage = _clamp(float(getattr(snapshot, 'volume_profile_coverage', 1.0) or 0.0))
    base = {
        'REVERSAL': 1.0, 'PULLBACK': 0.65,
        'BREAKOUT': 0.65, 'NEUTRAL_MOMENTUM': 0.85,
    }[archetype]
    side = selected['side']
    acceptance = (
        float(momentum.get('acceptance_long', 0.0) or 0.0)
        if side == 'LONG' else float(momentum.get('acceptance_short', 0.0) or 0.0)
    )
    aligned_momentum = max(
        0.0, (1.0 if side == 'LONG' else -1.0) * float(momentum.get('state', 0.0) or 0.0)
    )
    migration = _clamp(0.55 * acceptance + 0.45 * aligned_momentum)
    if archetype == 'BREAKOUT':
        base = 0.65 - 0.40 * migration
    movement = _clamp(float(getattr(snapshot, 'poc_movement_atr', 0.0) or 0.0) / 0.5)
    stability = 1.0 - 0.35 * movement
    return _clamp(base * (0.35 + 0.65 * freshness * coverage) * stability)


def score_side_calibrated(snapshot, setup, mode_info=None, artifact=None):
    base = v2.score_continuous(
        snapshot, setup, mode_info, live=False, include_poc_location=False,
    )
    side = base['selected_bias']
    archetype = calibration.archetype_for(base.get('mode'))
    regime = calibration.regime_for(base.get('mode'), getattr(snapshot, 'trend_m15', None))
    artifact = artifact or calibration.load_latest()
    profile = calibration.profile_for(artifact, side, archetype, regime)
    mode = _mode()
    bounded = bool(
        mode == 'BOUNDED_LIVE'
        and calibration.bounded_live_allowed(artifact, profile)
    )
    selected = base['sides'][side]
    raw_common_score = float(selected['score'])
    price = (
        (float(getattr(snapshot, 'best_bid', 0.0) or 0.0)
         + float(getattr(snapshot, 'best_ask', 0.0) or 0.0)) / 2.0
    )
    atr = max(float(getattr(snapshot, 'atr_1m', 0.0) or 0.0), 1e-9)
    poc = float(getattr(snapshot, 'poc', 0.0) or 0.0)
    side_sign = 1.0 if side == 'LONG' else -1.0
    signed_distance = side_sign * (poc - price) / atr if poc > 0.0 and price > 0.0 else 0.0
    relevance = _poc_relevance(
        snapshot, archetype, selected, base.get('momentum_breakdown', {}) or {}
    )
    weight = profile['poc_weight']
    poc_effect = relevance * (
        weight * math.tanh(max(signed_distance, 0.0) / 1.25)
        - 2.0 * weight * math.tanh(max(-signed_distance, 0.0) / 1.25)
    )
    horizon_reliability = profile.get('momentum_reliability', {}) or {}
    momentum_shape = sum(
        float(horizon_reliability.get(str(horizon), 1.0)) * weight
        for horizon, weight in ((15, 0.20), (60, 0.35), (180, 0.45))
    )
    family_reliability = profile.get('reversal_family_reliability', {}) or {}
    active_families = [
        member
        for row in selected.get('causal_components', ())
        if float(row.get('effect', 0.0) or 0.0) > 0.0
        for member in row.get('members', ())
        if member != 'M15_STRUCTURE'
    ]
    reversal_shape = (
        sum(float(family_reliability.get(name, 1.0)) for name in active_families)
        / len(active_families) if active_families else 1.0
    )
    reliability_shape = _clamp(
        0.50 * momentum_shape + 0.50 * reversal_shape, 0.85, 1.15
    )
    evidence_multiplier = _clamp(
        profile['evidence_multiplier'] * reliability_shape, 0.85, 1.15
    )
    adjusted_score = _clamp(
        50.0 + (raw_common_score - 50.0) * evidence_multiplier + poc_effect,
        0.0, 100.0,
    )
    confidence = _clamp(float(base['confidence']) * profile['confidence_multiplier'])
    activation = _clamp(float(base['activation']) * profile['activation_multiplier'])
    floor = _clamp(
        float(base['activation_floor']) * profile['floor_multiplier'], 25.2, 49.5
    )
    trade_power = adjusted_score * confidence * activation
    size = _clamp(
        float(base['target_notional_pct']) * profile['size_multiplier'], 0.0, 9.0
    )
    result = dict(base)
    result.update({
        'version': VERSION,
        'baseline_score': base['score'],
        'baseline_trade_power': base['trade_power'],
        'baseline_activation_floor': base['activation_floor'],
        'score': round(adjusted_score, 4),
        'confidence': round(confidence, 4),
        'activation': round(activation, 4),
        'trade_power': round(trade_power, 4),
        'activation_floor': round(floor, 4),
        'activated': bool(trade_power >= floor),
        'target_notional_pct': round(size, 4),
        'display_tier': v2._tier(adjusted_score),
        'raw_common_score': round(raw_common_score, 4),
        'side_adjusted_score': round(adjusted_score, 4),
        'side_profile': profile,
        'side_reliability_shape': round(reliability_shape, 6),
        'side_noise_scale': profile['noise_scale'],
        'poc_signed_distance_atr': round(signed_distance, 6),
        'poc_relevance': round(relevance, 6),
        'poc_effect': round(poc_effect, 6),
        'posterior_sample_size': profile['effective_sample_size'],
        'posterior_shrinkage': profile['parent_shrinkage'],
        'calibration_version': artifact.get('calibration_version'),
        'calibration_hash': artifact.get('artifact_hash'),
        'calibration_mode': mode,
        'bounded_live_eligible': calibration.bounded_live_allowed(artifact, profile),
        'bounded_live_applied': bounded,
        'shadow_trade_power': round(trade_power, 4),
        'shadow_activation_floor': round(floor, 4),
        'shadow_target_notional_pct': round(size, 4),
        'shadow_activated': bool(trade_power >= floor),
        'live_authority': False,
        'poc_location_included': False,
    })
    return result


def apply_bounded_live(base_result, side_result):
    """Apply only an already-approved bounded cell; otherwise exact V2 copy."""
    result = dict(base_result)
    if not side_result.get('bounded_live_applied'):
        return result
    if (
        base_result.get('snapshot_time') != side_result.get('snapshot_time')
        or base_result.get('selected_bias') != side_result.get('selected_bias')
    ):
        raise ValueError('side calibration does not match live V2 snapshot')
    result.update({
        'score': float(side_result['side_adjusted_score']),
        'confidence': float(side_result['confidence']),
        'activation': float(side_result['activation']),
        'trade_power': float(side_result['shadow_trade_power']),
        'activation_floor': float(side_result['shadow_activation_floor']),
        'activated': bool(side_result['shadow_activated']),
        'target_notional_pct': float(side_result['shadow_target_notional_pct']),
        'display_tier': v2._tier(float(side_result['side_adjusted_score'])),
        'raw_common_score': side_result['raw_common_score'],
        'side_adjusted_score': side_result['side_adjusted_score'],
        'side_profile': side_result['side_profile'],
        'side_noise_scale': side_result['side_noise_scale'],
        'poc_signed_distance_atr': side_result['poc_signed_distance_atr'],
        'poc_relevance': side_result['poc_relevance'],
        'poc_effect': side_result['poc_effect'],
        'posterior_sample_size': side_result['posterior_sample_size'],
        'posterior_shrinkage': side_result['posterior_shrinkage'],
        'calibration_version': side_result['calibration_version'],
        'calibration_hash': side_result['calibration_hash'],
        'calibration_mode': 'BOUNDED_LIVE',
        'bounded_live_applied': True,
        'live_authority': True,
    })
    result['confidence'] = _clamp(result['confidence'])
    result['activation'] = _clamp(result['activation'])
    result['power_margin'] = round(
        result['trade_power'] - result['activation_floor'], 4
    )
    return result
