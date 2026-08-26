"""Radar: quản lý ARMED_WINDOW và coalesce các lần re-score."""

import asyncio
import importlib.util
import logging
import os
import time
from pathlib import Path


CURRENT_DIR = Path(__file__).resolve().parent
PRE_ARM_MULT = 1.5
FULL_ARM_MULT = 0.5
ARM_RETENTION_MULT = 1.0
PRE_ARM_TTL_SECONDS = 30.0
FULL_ARM_DWELL_SECONDS = 0.10
PRE_ARM_ZONE_MOVE_MULT = 0.75
SWEEP_WAIT_SECONDS = 90.0
SWEEP_MIN_PENETRATION_ATR = 0.05
SWEEP_MAX_PENETRATION_ATR = 1.50
RECLAIM_CONFIRM_ATR = 0.10
RECLAIM_HOLD_SECONDS = 0.30
ARMED_TTL_SECONDS = 60.0
NEUTRAL_MOMENTUM_TTL_SECONDS = max(
    ARMED_TTL_SECONDS,
    float(os.getenv('SMC_NEUTRAL_OPPORTUNITY_TTL_SECONDS', '900.0')),
)
RESCORE_MIN_INTERVAL = 0.2
RESCORE_FALLBACK_INTERVAL = 1.0
# Depth evidence itself arrives at 100 ms. Faster polling only re-reads the
# same mutable state and burns CPU; scoring still has its independent 5 Hz cap.
RADAR_LOOP_INTERVAL = float(os.getenv('SMC_RADAR_LOOP_SECONDS', '0.10'))
SHADOW_HISTORY_INTERVAL = float(
    os.getenv('SMC_SHADOW_HISTORY_SECONDS', '1.0')
)
SHADOW_HISTORY_LIMIT = int(os.getenv('SMC_SHADOW_HISTORY_LIMIT', '180'))
SIDE_SHADOW_SCORE_INTERVAL = float(
    os.getenv('SMC_SIDE_SHADOW_SCORE_SECONDS', '1.0')
)
SETUP_FOLLOWUP_SECONDS = 2700.0
SETUP_FOLLOWUP_CHECKPOINTS = (30, 120, 900, 1800, 2700)
SETUP_FOLLOWUP_COST_BPS = (
    float(os.getenv('SMC_SHADOW_ENTRY_FEE_BPS', '4.0'))
    + float(os.getenv('SMC_SHADOW_EXIT_FEE_BPS', '4.0'))
)
BREAKOUT_EVENT_TTL_SECONDS = 15.0
BREAKOUT_EVENT_TOMBSTONE_LIMIT = 512
BREAKOUT_OPPORTUNITY_LIMIT = 256
BREAKOUT_OPPORTUNITY_TTL_SECONDS = 2700.0
BREAKOUT_RETEST_BAND_ATR = 0.35
BREAKOUT_RETEST_HOLD_SECONDS = 0.30
BREAKOUT_FAILURE_ATR = 0.50
BREAKOUT_CHASE_WAIT_SECONDS = 3.0


def _watch_state():
    return 'WATCH' if str(os.getenv('SMC_SCORER_VERSION', '')).upper() in (
        'CONTINUOUS_V1', 'CONTINUOUS_V2',
    ) else 'ARMED_WINDOW'


def _continuous_v2_enabled():
    return str(os.getenv('SMC_SCORER_VERSION', '')).upper() == 'CONTINUOUS_V2'


def load_module(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chi_huy_truong = load_module(
    "chi_huy_truong", CURRENT_DIR / "tong_ket_chi_huy" / "chi_huy_truong.py"
)
snapshot_mod = load_module(
    "decision_snapshot", CURRENT_DIR / "tong_ket_chi_huy" / "decision_snapshot.py"
)
reversal_context = load_module(
    "reversal_context", CURRENT_DIR / "tong_ket_chi_huy" / "reversal_context.py"
)
trend_context = load_module(
    "trend_context", CURRENT_DIR / "tong_ket_chi_huy" / "trend_context.py"
)
breakout_policy = load_module(
    "chinh_sach_breakout", CURRENT_DIR / "tong_ket_chi_huy" / "chinh_sach_breakout.py"
)
strategy_profile = load_module(
    "strategy_profile_radar", CURRENT_DIR.parent / "loi_he_thong" / "strategy_profile.py"
)
continuous_scorer = load_module(
    "cham_diem_continuous",
    CURRENT_DIR / "tong_ket_chi_huy" / "cham_diem_continuous.py",
)
continuous_scorer_v2 = load_module(
    "cham_diem_continuous_v2",
    CURRENT_DIR / "tong_ket_chi_huy" / "cham_diem_continuous_v2.py",
)
side_scorer_v21 = load_module(
    "cham_diem_continuous_v21",
    CURRENT_DIR / "tong_ket_chi_huy" / "cham_diem_continuous_v21.py",
)
ml_meta_scout = load_module(
    "ml_meta_scout",
    CURRENT_DIR.parent / "4_nghien_cuu_ai" / "ml_meta" / "scout.py",
)
ml_meta_artifact = load_module(
    "ml_meta_artifact",
    CURRENT_DIR.parent / "4_nghien_cuu_ai" / "ml_meta" / "artifact.py",
)


def _queue_ml_meta(state, payload, now_wall=None):
    queue = getattr(state, 'ml_meta_events', None)
    if queue is None:
        return False
    if queue.maxlen is not None and len(queue) >= queue.maxlen:
        state.ml_meta_drop_count = int(getattr(state, 'ml_meta_drop_count', 0)) + 1
        return False
    queue.append({
        'ts': time.time() if now_wall is None else float(now_wall),
        'event': 'ML_META_ACTION_STATE',
        'run_id': getattr(state, 'run_id', None),
        'payload': payload,
    })
    return True


def _collect_ml_meta_scout(
    state, active_scorer, mode_info, now_wall, now_mono,
):
    """Collect both sides at 1 Hz; never mutate setup/Commander/execution state."""
    if ml_meta_artifact.requested_mode() == 'OFF':
        return
    if now_mono - float(getattr(state, 'ml_meta_last_collect_mono', 0.0)) < 1.0:
        return
    modes = [
        str(item) for item in list((mode_info or {}).get('modes', ()) or ())
        if str(item).upper() != 'STANDBY'
    ]
    if not modes or not getattr(state, 'system_ready', False):
        return
    state.ml_meta_last_collect_mono = now_mono
    mode = modes[0]
    tick_size = float(
        (getattr(state, 'exchange_filters', {}) or {}).get('tick_size', 0.1)
        or 0.1
    )
    base_snapshot = snapshot_mod.capture(state, None, now_wall, now_mono)
    rows = []
    for side in ml_meta_scout.SIDES:
        anchor = ml_meta_scout.select_anchor(base_snapshot, side)
        mid = (
            (float(base_snapshot.best_bid) + float(base_snapshot.best_ask)) / 2.0
        )
        zone = float(anchor.get('price') or mid)
        opportunity_id = ml_meta_scout.opportunity_identity(
            base_snapshot, side, anchor, tick_size
        )
        setup = {
            'setup_id': f'SCOUT:{side}', 'semantic_key': opportunity_id,
            'opportunity_id': opportunity_id, 'generation': 0,
            'mode': mode, 'bias': side, 'zone': zone, 'kind': 'scout',
        }
        decision = snapshot_mod.capture(state, setup, now_wall, now_mono)
        score = active_scorer.score_continuous(
            decision, setup, mode_info, live=False
        )
        side_rows = ml_meta_scout.build_rows(
            decision, score, mode, tick_size,
            run_id=getattr(state, 'run_id', None),
            code_version=getattr(state, 'code_version', None),
        )
        row = next(item for item in side_rows if item['side'] == side)
        row['event_type'] = 'OPPORTUNITY_STATE'
        row['payload_hash'] = ml_meta_scout.row_hash(row)
        rows.append(row)
    registry = getattr(state, 'ml_meta_registry', None)
    if not isinstance(registry, dict):
        registry = {}
        state.ml_meta_registry = registry
    for row in rows:
        side = row['side']
        previous = registry.get(side)
        if previous and previous.get('opportunity_id') != row['opportunity_id']:
            terminal = dict(previous)
            terminal.update({
                'event_type': 'OPPORTUNITY_TERMINAL',
                'terminal_label': 'SUPERSEDED_STRUCTURE',
                'terminal_time': now_wall,
            })
            terminal['payload_hash'] = ml_meta_scout.row_hash(terminal)
            _queue_ml_meta(state, terminal, now_wall)
        registry[side] = {
            'opportunity_id': row['opportunity_id'], 'side': side,
            'first_seen': (
                previous.get('first_seen', now_wall)
                if previous and previous.get('opportunity_id') == row['opportunity_id']
                else now_wall
            ),
            'last_seen': now_wall, 'last_payload_hash': row['payload_hash'],
            'run_id': getattr(state, 'run_id', None),
        }
        _queue_ml_meta(state, row, now_wall)


def _continuous_shadow_enabled():
    return str(os.getenv('SMC_CONTINUOUS_SHADOW', '1')).strip().lower() not in (
        '0', 'false', 'off', 'no',
    )


def _continuous_opportunity_key(setup):
    return str(
        setup.get('opportunity_id') or setup.get('semantic_key')
        or setup.get('setup_id')
    )


def _queue_continuous_shadow(state, payload, now_wall=None):
    queue = getattr(state, 'continuous_shadow_events', None)
    if queue is None:
        return False
    if queue.maxlen is not None and len(queue) >= queue.maxlen:
        state.continuous_shadow_drop_count = int(
            getattr(state, 'continuous_shadow_drop_count', 0)
        ) + 1
        return False
    queue.append({
        'ts': time.time() if now_wall is None else float(now_wall),
        'event': 'CONTINUOUS_SCORE_SHADOW',
        'run_id': getattr(state, 'run_id', None),
        'position_cycle_id': None,
        'payload': payload,
    })
    return True


def _side_calibration_shadow_enabled():
    return str(os.getenv('SMC_SIDE_CALIBRATION_MODE', 'SHADOW')).upper() in (
        'SHADOW', 'BOUNDED_LIVE',
    )


def _record_side_calibration_shadow(state, setup, result, now_wall):
    """One registry row per opportunity; re-scores are causal history only."""
    opportunity_id = _continuous_opportunity_key(setup)
    registry = getattr(state, 'side_calibration_shadow_registry', None)
    if not isinstance(registry, dict):
        registry = {}
        state.side_calibration_shadow_registry = registry
    record = registry.get(opportunity_id)
    first = record is None
    if record is None:
        record = {
            'opportunity_id': opportunity_id,
            'run_id': getattr(state, 'run_id', None),
            'side': result['selected_bias'],
            'archetype': result['side_profile']['archetype'],
            'regime': result['side_profile']['regime'],
            'first_seen_at': now_wall, 'last_seen_at': now_wall,
            'sample_weight': 1, 'history': [], 'terminal': None,
        }
        registry[opportunity_id] = record
        while len(registry) > 512:
            oldest = min(registry, key=lambda key: registry[key]['first_seen_at'])
            if oldest == opportunity_id and len(registry) == 1:
                break
            registry.pop(oldest, None)
    record['last_seen_at'] = now_wall
    record['last'] = {
        key: result.get(key) for key in (
            'raw_common_score', 'side_adjusted_score', 'shadow_trade_power',
            'shadow_activation_floor', 'shadow_target_notional_pct',
            'shadow_activated', 'poc_signed_distance_atr', 'poc_relevance',
            'poc_effect', 'posterior_sample_size', 'posterior_shrinkage',
            'calibration_version', 'calibration_hash', 'calibration_mode',
        )
    }
    previous_emit = float(record.get('last_emit_at', 0.0) or 0.0)
    previous_score = float(record.get('last_emitted_score', -999.0) or -999.0)
    full = bool(
        first
        or abs(float(result['side_adjusted_score']) - previous_score) >= 1.0
        or bool(result['shadow_activated']) != bool(record.get('last_emitted_activated'))
        or result.get('calibration_hash') != record.get('last_emitted_calibration_hash')
    )
    activation_changed = bool(result['shadow_activated']) != bool(
        record.get('last_emitted_activated')
    )
    last_history = float(record.get('last_history_at', 0.0) or 0.0)
    if first or activation_changed or now_wall - last_history >= SHADOW_HISTORY_INTERVAL:
        history = record['history']
        history.append({'ts': now_wall, **record['last']})
        del history[:-SHADOW_HISTORY_LIMIT]
        record['last_history_at'] = now_wall
    if not first and not activation_changed and now_wall - previous_emit < 1.0:
        return
    record['last_emit_at'] = now_wall
    record['last_emitted_score'] = float(result['side_adjusted_score'])
    record['last_emitted_activated'] = bool(result['shadow_activated'])
    record['last_emitted_calibration_hash'] = result.get('calibration_hash')
    _queue_continuous_shadow(state, {
        'analysis_type': 'SIDE_SCORE' if full else 'SIDE_HEARTBEAT',
        'version': result['version'], 'run_id': getattr(state, 'run_id', None),
        'opportunity_id': opportunity_id, 'setup_id': setup.get('setup_id'),
        'setup_generation': int(setup.get('generation', 0) or 0),
        'side': result['selected_bias'], 'mode': setup.get('mode'),
        'sample_weight': 1, 'rescore_is_new_sample': False,
        'live_authority': False,
        'breakdown': result if full else record['last'],
    }, now_wall)


def _shadow_economic_input(decision, setup, result):
    return {
        'snapshot_time': float(decision.snapshot_time),
        'side': result['selected_bias'],
        'size_pct': float(result.get('target_notional_pct', 0.0) or 0.0),
        'balance_usdt': float(getattr(decision, 'balance_usdt', 0.0) or 0.0),
        'exchange_filters': dict(getattr(decision, 'exchange_filters', {}) or {}),
        'best_bid': float(getattr(decision, 'best_bid', 0.0) or 0.0),
        'best_ask': float(getattr(decision, 'best_ask', 0.0) or 0.0),
        'bids_top_10': list(getattr(decision, 'bids_top_10', ()) or ()),
        'asks_top_10': list(getattr(decision, 'asks_top_10', ()) or ()),
        'atr_1m': float(getattr(decision, 'atr_1m', 0.0) or 0.0),
        'poc': float(getattr(decision, 'poc', 0.0) or 0.0),
        'vah': float(getattr(decision, 'vah', 0.0) or 0.0),
        'val': float(getattr(decision, 'val', 0.0) or 0.0),
        'swing_high_m15': float(getattr(decision, 'swing_high_m15', 0.0) or 0.0),
        'swing_low_m15': float(getattr(decision, 'swing_low_m15', 0.0) or 0.0),
        'sweep_m1': dict(getattr(decision, 'sweep_m1', {}) or {}),
        'mode': setup.get('mode'),
        'setup_zone': float(setup.get('zone', 0.0) or 0.0),
        'setup_kind': setup.get('kind'),
        'entry_style': setup.get('entry_style'),
        'breakout_target': float(setup.get('breakout_target', 0.0) or 0.0),
        'breakout_target2': float(setup.get('breakout_target2', 0.0) or 0.0),
        'breakout_target_basis': setup.get('breakout_target_basis'),
    }


def _record_continuous_shadow_score(
    state, setup, decision, result, now_wall, now_mono,
):
    opportunity_id = _continuous_opportunity_key(setup)
    registry = getattr(state, 'continuous_shadow_registry', None)
    if not isinstance(registry, dict):
        registry = {}
        state.continuous_shadow_registry = registry
    record = registry.get(opportunity_id)
    first = record is None
    if record is None:
        record = {
            'opportunity_id': opportunity_id,
            'mode': setup.get('mode'), 'bias': setup.get('bias'),
            'first_seen_at': now_wall, 'last_seen_at': now_wall,
            'setup_ids': [], 'sample_count': 0, 'history': [],
            'activated': False, 'activation_at': None,
            'terminal': None, 'economics': None,
        }
        registry[opportunity_id] = record
        while len(registry) > 128:
            terminal_key = next(
                (key for key, item in registry.items() if item.get('terminal')),
                next(iter(registry)),
            )
            if terminal_key == opportunity_id and len(registry) == 1:
                break
            registry.pop(terminal_key, None)

    setup_id = setup.get('setup_id')
    if setup_id and setup_id not in record['setup_ids']:
        record['setup_ids'].append(setup_id)
        record['setup_ids'] = record['setup_ids'][-16:]
    previous_score = float(record.get('last_score', 0.0) or 0.0)
    previous_power = float(record.get('last_trade_power', 0.0) or 0.0)
    previous_tier = record.get('last_tier')
    previously_activated = bool(record.get('activated'))
    activated = bool(result.get('activated'))
    if activated and not previously_activated:
        record['activation_at'] = now_wall
    record['activated'] = bool(previously_activated or activated)
    record['last_seen_at'] = now_wall
    record['evaluation_count'] = int(record.get('evaluation_count', 0)) + 1
    record['max_score'] = max(float(record.get('max_score', 0.0)), float(result['score']))
    record['max_trade_power'] = max(
        float(record.get('max_trade_power', 0.0)), float(result['trade_power'])
    )
    record['last_score'] = float(result['score'])
    record['last_trade_power'] = float(result['trade_power'])
    record['last_confidence'] = float(result['confidence'])
    record['last_activation'] = float(result['activation'])
    record['last_tier'] = result['display_tier']
    record['last_target_notional_pct'] = float(result['target_notional_pct'])
    record['last_source_event_ids'] = list(result.get('source_event_ids', ()))
    last_emit = float(record.get('last_emit_at', 0.0) or 0.0)
    full = bool(
        first or abs(result['score'] - previous_score) >= 1.0
        or abs(result['trade_power'] - previous_power) >= 2.0
        or result['display_tier'] != previous_tier
        or activated != previously_activated
    )
    heartbeat = now_wall - last_emit >= 1.0
    economic_requested = bool(
        activated and (
            not record.get('economic_requested_at')
            or abs(result['trade_power'] - float(record.get('economic_request_power', 0.0))) >= 5.0
        )
    )
    if economic_requested:
        record['economic_requested_at'] = now_wall
        record['economic_request_power'] = float(result['trade_power'])
    last_history = float(record.get('last_history_at', 0.0) or 0.0)
    if (
        first or activated != previously_activated
        or now_wall - last_history >= SHADOW_HISTORY_INTERVAL
    ):
        history = record.setdefault('history', [])
        history.append({
            'ts': now_wall, 'score': result['score'],
            'confidence': result['confidence'], 'activation': result['activation'],
            'trade_power': result['trade_power'], 'activated': activated,
        })
        del history[:-SHADOW_HISTORY_LIMIT]
        record['last_history_at'] = now_wall
        record['sample_count'] = len(history)
    # A causal 1 Hz trace is sufficient for 15/60/180-second features.  Only
    # activation/economics transitions may bypass it; score jitter must not
    # create multi-megabyte journal churn.
    urgent = bool(first or activated != previously_activated or economic_requested)
    if not urgent and not heartbeat:
        return
    record['last_emit_at'] = now_wall
    payload = {
        'analysis_type': (
            'LIVE_SCORE' if result.get('live_authority') and full
            else 'SCORE' if full else 'HEARTBEAT'
        ),
        'version': result['version'], 'opportunity_id': opportunity_id,
        'setup_id': setup_id, 'setup_generation': int(setup.get('generation', 0)),
        'mode': setup.get('mode'), 'bias': setup.get('bias'),
        'score': result['score'], 'confidence': result['confidence'],
        'activation': result['activation'], 'trade_power': result['trade_power'],
        'activation_floor': result['activation_floor'],
        'activated': activated, 'display_tier': result['display_tier'],
        'target_notional_pct': result['target_notional_pct'],
        'allocation_unit': result['allocation_unit'],
        'source_event_ids': list(result.get('source_event_ids', ())),
        'evidence_quality_flags': list(result.get('evidence_quality_flags', ())),
        'economic_requested': economic_requested,
        'live_authority': bool(result.get('live_authority')),
    }
    if full:
        payload['breakdown'] = result
    if economic_requested:
        payload['economic_input'] = _shadow_economic_input(decision, setup, result)
    _queue_continuous_shadow(state, payload, now_wall)


def _finalize_continuous_shadow(state, setup, outcome, now_wall):
    opportunity_id = _continuous_opportunity_key(setup)
    registry = getattr(state, 'continuous_shadow_registry', {}) or {}
    record = registry.get(opportunity_id)
    if record is None:
        return
    record['terminal'] = {
        'state': outcome.get('terminal_state'), 'reason': outcome.get('reason'),
        'ended_at': now_wall, 'setup_id': outcome.get('setup_id'),
    }
    getattr(state, 'continuous_shadow_schedule', {}).pop(opportunity_id, None)
    _queue_continuous_shadow(state, {
        'analysis_type': 'TERMINAL', 'version': 'CONTINUOUS_SHADOW_V1',
        'opportunity_id': opportunity_id, 'setup_id': outcome.get('setup_id'),
        'terminal': dict(record['terminal']), 'live_authority': False,
    }, now_wall)
    side_record = getattr(state, 'side_calibration_shadow_registry', {}).get(
        opportunity_id
    )
    if side_record is not None:
        side_record['terminal'] = {
            'state': outcome.get('terminal_state'),
            'reason': outcome.get('reason'), 'ended_at': now_wall,
            'setup_id': outcome.get('setup_id'),
        }
        _queue_continuous_shadow(state, {
            'analysis_type': 'SIDE_TERMINAL',
            'version': side_scorer_v21.VERSION,
            'run_id': getattr(state, 'run_id', None),
            'opportunity_id': opportunity_id,
            'terminal': dict(side_record['terminal']),
            'sample_weight': 1, 'live_authority': False,
        }, now_wall)


def _emit_radar_event(state, event, payload):
    """Non-blocking decision tap; journal persistence runs in another task."""
    if hasattr(state, 'journal_events'):
        state.journal_events.append({
            'ts': time.time(),
            'event': event,
            'run_id': getattr(state, 'run_id', None),
            'position_cycle_id': None,
            'payload': payload,
        })


def _zone_bounds(zone_level, atr, multiplier):
    return zone_level - multiplier * atr, zone_level + multiplier * atr


def _in_full_zone(current_price, zone_level, atr):
    if atr <= 0.0 or zone_level <= 0.0 or current_price <= 0.0:
        return False
    lo, hi = _zone_bounds(zone_level, atr, FULL_ARM_MULT)
    return lo <= current_price <= hi


def _in_retention_zone(current_price, zone_level, atr):
    """Hysteresis: đã ARM thì cho giá dao động rộng hơn vùng kích hoạt."""
    if atr <= 0.0 or zone_level <= 0.0 or current_price <= 0.0:
        return False
    lo, hi = _zone_bounds(zone_level, atr, ARM_RETENTION_MULT)
    return lo <= current_price <= hi


def _opposing_breakout(state, bias, now_wall):
    event = getattr(state, 'breakout_m1', {}) or {}
    return bool(
        event.get('flag')
        and now_wall - float(event.get('ts', 0.0)) <= BREAKOUT_EVENT_TTL_SECONDS
        and event.get('direction') in ('LONG', 'SHORT')
        and event.get('direction') != bias
    )


def _fresh_breakout_event(state, now_wall):
    event = getattr(state, 'breakout_m1', {}) or {}
    return event if (
        event.get('flag')
        and event.get('direction') in ('LONG', 'SHORT')
        and float(event.get('level', 0.0) or 0.0) > 0.0
        and 0.0 <= now_wall - float(event.get('ts', 0.0)) <= BREAKOUT_EVENT_TTL_SECONDS
    ) else None


def check_arm_state(current_price, prev_price, zone_level, atr, direction_bias):
    if atr <= 0.0 or zone_level <= 0.0 or current_price <= 0.0:
        return "IDLE"
    pre_lo, pre_hi = _zone_bounds(zone_level, atr, PRE_ARM_MULT)
    _, full_hi = _zone_bounds(zone_level, atr, FULL_ARM_MULT)
    full_lo, _ = _zone_bounds(zone_level, atr, FULL_ARM_MULT)
    in_pre = pre_lo <= current_price <= pre_hi
    in_full = _in_full_zone(current_price, zone_level, atr)
    if prev_price is None or prev_price <= 0.0:
        return "PRE_ARM" if in_pre else "IDLE"
    crossing = (
        prev_price > full_hi >= current_price
        if direction_bias == "LONG"
        else prev_price < full_lo <= current_price
    )
    if in_full and crossing:
        return "FULL_ARM"
    return "PRE_ARM" if in_pre else "IDLE"


def _advance_sweep_wait(probe, current_price, previous, now_mono):
    """Expired PRE_ARM becomes one bounded sweep/reclaim opportunity.

    This is event-driven, not an extended generic TTL: no sweep means no arm;
    a deep acceptance invalidates; a one-tick reclaim cannot confirm.
    """
    zone = float(probe['zone'])
    atr = float(probe['atr'])
    bias = probe['bias']
    pre_lo, pre_hi = _zone_bounds(zone, atr, PRE_ARM_MULT)
    if probe.get('terminal_wait_exit'):
        if pre_lo <= current_price <= pre_hi:
            return 'SWEEP_WAIT_COOLDOWN', probe, 'SWEEP_WAIT_TOMBSTONE'
        return 'IDLE', None, 'LEFT_SWEEP_WAIT_TOMBSTONE'
    expired_at = float(probe.get('expired_at_mono', now_mono))
    if now_mono - expired_at > SWEEP_WAIT_SECONDS:
        if pre_lo <= current_price <= pre_hi:
            probe['state'] = 'SWEEP_WAIT_COOLDOWN'
            probe['terminal_wait_exit'] = True
            return 'SWEEP_WAIT_COOLDOWN', probe, 'SWEEP_WAIT_EXPIRED'
        return 'IDLE', None, 'SWEEP_WAIT_EXPIRED_OUTSIDE_ZONE'

    penetration_atr = (
        (zone - current_price) / atr
        if bias == 'LONG' else (current_price - zone) / atr
    )
    if penetration_atr > SWEEP_MAX_PENETRATION_ATR:
        return 'IDLE', None, 'SWEEP_TOO_DEEP'

    newly_swept = False
    if penetration_atr >= SWEEP_MIN_PENETRATION_ATR:
        newly_swept = not bool(probe.get('sweep_seen'))
        probe['sweep_seen'] = True
        probe['sweep_seen_mono'] = float(
            probe.get('sweep_seen_mono', now_mono) or now_mono
        )
        extreme = float(probe.get('sweep_extreme', current_price) or current_price)
        probe['sweep_extreme'] = (
            min(extreme, current_price) if bias == 'LONG'
            else max(extreme, current_price)
        )
        probe['max_penetration_atr'] = max(
            float(probe.get('max_penetration_atr', 0.0) or 0.0),
            penetration_atr,
        )

    reclaim_level = (
        zone + RECLAIM_CONFIRM_ATR * atr
        if bias == 'LONG' else zone - RECLAIM_CONFIRM_ATR * atr
    )
    reclaimed = (
        current_price >= reclaim_level
        if bias == 'LONG' else current_price <= reclaim_level
    )
    if probe.get('sweep_seen') and reclaimed:
        reclaim_since = probe.get('reclaim_since_mono')
        if reclaim_since is None:
            probe['reclaim_since_mono'] = now_mono
            probe['state'] = 'SWEEP_WAIT'
            return 'SWEEP_WAIT', probe, 'RECLAIM_HOLD'
        if now_mono - float(reclaim_since) >= RECLAIM_HOLD_SECONDS:
            probe['state'] = 'FULL_ARM'
            probe['reclaim_confirmed_mono'] = now_mono
            return 'FULL_ARM', probe, 'SWEEP_RECLAIM_CONFIRMED'
        return 'SWEEP_WAIT', probe, 'RECLAIM_HOLD'
    probe['reclaim_since_mono'] = None

    if not pre_lo <= current_price <= pre_hi:
        return 'IDLE', None, 'LEFT_SWEEP_WAIT'
    probe['state'] = 'SWEEP_WAIT'
    return (
        'SWEEP_WAIT', probe,
        'SWEEP_DETECTED' if newly_swept else 'WAITING_SWEEP_OR_RECLAIM',
    )


def advance_arm_probe(
    current_price, prev_price, zone_level, atr, direction_bias,
    probe=None, now_mono=None,
):
    """Stateful activation that freezes a zone from PRE_ARM until trigger/reset.

    A direct directional crossing still arms immediately. If the candidate appears
    while price is already inside/past the FULL band, a short level dwell recovers
    the touch as long as price remains inside the retention band. CORE/VETO remain
    downstream and are intentionally not changed here.
    """
    now_mono = time.monotonic() if now_mono is None else float(now_mono)
    current_price = float(current_price or 0.0)
    previous = float(prev_price or 0.0)
    zone_level = float(zone_level or 0.0)
    atr = float(atr or 0.0)
    if (
        current_price <= 0.0 or zone_level <= 0.0 or atr <= 0.0
        or direction_bias not in ('LONG', 'SHORT')
    ):
        return 'IDLE', None, 'INVALID_INPUT'

    reset_reason = None
    if probe is not None:
        frozen_atr = float(probe.get('atr', atr) or atr)
        move_limit = PRE_ARM_ZONE_MOVE_MULT * max(atr, frozen_atr)
        if abs(zone_level - float(probe.get('zone', zone_level))) > move_limit:
            probe = None
            reset_reason = 'ZONE_MOVED_RESTART'
        elif probe.get('expired'):
            return _advance_sweep_wait(
                probe, current_price, previous, now_mono
            )
        elif now_mono - float(probe.get('started_mono', now_mono)) > PRE_ARM_TTL_SECONDS:
            # Hết TTL không xóa cơ hội đúng lúc giá đang test vùng. Chuyển sang
            # một lifecycle bounded chỉ cho phép sweep -> reclaim xác nhận.
            probe['expired'] = True
            probe['expired_at_mono'] = now_mono
            probe['state'] = 'SWEEP_WAIT'
            return (
                'SWEEP_WAIT', probe,
                'SWEEP_WAIT_STARTED',
            )

    if probe is None:
        pre_lo, pre_hi = _zone_bounds(zone_level, atr, PRE_ARM_MULT)
        if not pre_lo <= current_price <= pre_hi:
            return 'IDLE', None, reset_reason or 'OUTSIDE_PRE'
        directional = bool(previous > 0.0) and (
            current_price < previous
            if direction_bias == 'LONG'
            else current_price > previous
        )
        probe = {
            'state': 'PRE_ARM',
            'zone': zone_level,
            'live_zone': zone_level,
            'atr': atr,
            'bias': direction_bias,
            'started_mono': now_mono,
            'activation_since_mono': None,
            'approach_valid': directional,
        }
        created_reason = reset_reason or 'ENTERED_PRE'
    else:
        created_reason = None
        probe['live_zone'] = zone_level
        if previous > 0.0:
            directional = (
                current_price < previous
                if direction_bias == 'LONG'
                else current_price > previous
            )
            probe['approach_valid'] = bool(probe.get('approach_valid')) or directional

    frozen_zone = float(probe['zone'])
    frozen_atr = float(probe['atr'])
    pre_lo, pre_hi = _zone_bounds(frozen_zone, frozen_atr, PRE_ARM_MULT)
    full_lo, full_hi = _zone_bounds(frozen_zone, frozen_atr, FULL_ARM_MULT)
    retention_lo, retention_hi = _zone_bounds(
        frozen_zone, frozen_atr, ARM_RETENTION_MULT
    )
    if not pre_lo <= current_price <= pre_hi:
        return 'IDLE', None, 'LEFT_PRE'

    crossing = bool(previous > 0.0) and (
        previous > full_hi >= current_price
        if direction_bias == 'LONG'
        else previous < full_lo <= current_price
    )
    in_activation_band = (
        retention_lo <= current_price <= full_hi
        if direction_bias == 'LONG'
        else full_lo <= current_price <= retention_hi
    )
    if crossing and in_activation_band:
        probe['state'] = 'FULL_ARM'
        return 'FULL_ARM', probe, 'DIRECTIONAL_CROSSING'

    if not in_activation_band or not probe.get('approach_valid'):
        probe['activation_since_mono'] = None
        reason = 'WAITING_APPROACH' if in_activation_band else 'WAITING_FULL_BAND'
        return 'PRE_ARM', probe, created_reason or reason

    activation_since = probe.get('activation_since_mono')
    if activation_since is None:
        probe['activation_since_mono'] = now_mono
        return 'PRE_ARM', probe, created_reason or 'FULL_BAND_DWELL'
    if now_mono - float(activation_since) >= FULL_ARM_DWELL_SECONDS:
        probe['state'] = 'FULL_ARM'
        in_full = full_lo <= current_price <= full_hi
        return 'FULL_ARM', probe, 'LEVEL_CONFIRMED' if in_full else 'GAP_RECOVERED'
    return 'PRE_ARM', probe, 'FULL_BAND_DWELL'


def build_candidates(mode_info, breakout_event=None):
    """Biến mode thành target độc lập với zone_id ổn định."""
    candidates = []
    modes = mode_info.get("modes", [])
    bias = mode_info.get("bias", "NONE")
    event = breakout_event or {}
    event_direction = event.get('direction')
    event_level = float(event.get('level', 0.0) or 0.0)
    event_id = event.get('event_id')
    lanes = list(mode_info.get('candidate_lanes', []) or [])
    if not lanes and bias in ('LONG', 'SHORT'):
        lanes = [{
            'bias': bias,
            'pullback_zones': mode_info.get('pullback_zones', []),
            'breakout_level': mode_info.get('breakout_level', 0.0),
        }]
    for pullback_mode in ("TREND-PULLBACK", "TRANSITION-PULLBACK"):
        if pullback_mode not in modes:
            continue
        for lane in lanes:
            lane_bias = lane.get('bias')
            if lane_bias not in ('LONG', 'SHORT'):
                continue
            for index, zone in enumerate(lane.get('pullback_zones', [])):
                if zone and zone > 0:
                    key = f"{pullback_mode}:{lane_bias}:{index}"
                    candidates.append({
                        "key": key, "zone_id": key, "mode": pullback_mode,
                        "bias": lane_bias, "zone": float(zone), "kind": "zone",
                    })
    # Trend-side value migration uses the existing continuous momentum lane:
    # the boundary only supplies location, while 15/60/180s acceptance and
    # independent live evidence retain all entry authority. It is maker-only.
    for item in mode_info.get('adaptive_retest_zones', []) or []:
        lane_bias = str(item.get('bias') or '').upper()
        zone = float(item.get('zone', 0.0) or 0.0)
        boundary = str(item.get('boundary') or '').upper()
        if lane_bias not in ('LONG', 'SHORT') or zone <= 0.0:
            continue
        key = f"VALUE-MIGRATION:{lane_bias}:{boundary}"
        candidates.append({
            'key': key, 'zone_id': key,
            'mode': 'NEUTRAL-MOMENTUM', 'bias': lane_bias,
            'zone': zone, 'kind': 'zone',
            'entry_style': 'PASSIVE_RETEST',
            'passive_entry_price': zone,
            'value_migration_retest': True,
            'value_boundary': boundary,
            'location_role': item.get('role') or 'VALUE_MIGRATION_RETEST',
        })
    if "TREND-BREAKOUT" in modes:
        for lane in lanes:
            lane_bias = lane.get('bias')
            level = float(lane.get("breakout_level", 0.0) or 0.0)
            if lane_bias not in ('LONG', 'SHORT') or level <= 0.0:
                continue
            key = f"TREND-BREAKOUT:{lane_bias}"
            candidates.append({
                "key": key, "zone_id": key, "mode": "TREND-BREAKOUT",
                "bias": lane_bias, "zone": level, "kind": "breakout",
                "breakout_event_id": (
                    event_id if event_direction == lane_bias else None
                ),
            })
    if "TRANSITION-BREAKOUT" in modes and bias in ("LONG", "SHORT"):
        level = float(mode_info.get("breakout_level", 0.0) or 0.0)
        if level > 0.0:
            key = f"TRANSITION-BREAKOUT:{bias}"
            candidates.append({
                "key": key, "zone_id": key,
                "mode": "TRANSITION-BREAKOUT", "bias": bias,
                "zone": level, "kind": "breakout",
                "breakout_event_id": mode_info.get("breakout_event_id"),
                "advisory_only": bool(mode_info.get("advisory_only", False)),
            })
    if "NEUTRAL-FADE" in modes:
        for bias_name, field in (("LONG", "zone_long"), ("SHORT", "zone_short")):
            zone = float(mode_info.get(field, 0.0) or 0.0)
            if zone > 0:
                key = f"NEUTRAL-FADE:{bias_name}"
                candidates.append({
                    "key": key, "zone_id": key, "mode": "NEUTRAL-FADE",
                    "bias": bias_name, "zone": zone, "kind": "zone",
                })
        if _continuous_v2_enabled():
            # Fade lanes cover LONG@VAL and SHORT@VAH. V2 adds the opposite
            # acceptance lanes so a neutral range can become momentum without
            # waiting for the next categorical M15 mode.
            for bias_name, field, boundary in (
                ('LONG', 'zone_short', 'VAH'),
                ('SHORT', 'zone_long', 'VAL'),
            ):
                zone = float(mode_info.get(field, 0.0) or 0.0)
                if zone > 0.0:
                    key = f"NEUTRAL-MOMENTUM:{bias_name}:{boundary}"
                    candidates.append({
                        'key': key, 'zone_id': key,
                        'mode': 'NEUTRAL-MOMENTUM', 'bias': bias_name,
                        'zone': zone, 'kind': 'zone',
                        'entry_style': 'PASSIVE_RETEST',
                        'passive_entry_price': zone,
                    })
    # Breakout được detect độc lập với bias cũ. Nếu mode hiện tại chưa có
    # candidate breakout cùng hướng, mở lane transition có size cap riêng.
    already_covered = any(
        candidate['kind'] == 'breakout'
        and candidate['bias'] == event_direction
        for candidate in candidates
    )
    if (
        event.get('flag')
        and event_direction in ('LONG', 'SHORT')
        and event_level > 0.0
        and not already_covered
    ):
        key = f"TRANSITION-BREAKOUT:{event_direction}"
        candidates.append({
            'key': key, 'zone_id': key, 'mode': 'TRANSITION-BREAKOUT',
            'bias': event_direction, 'zone': event_level, 'kind': 'breakout',
            'breakout_event_id': event_id,
        })
    return candidates


def semantic_setup_key(candidate, structure_version):
    """Danh tính cơ hội không đổi khi Radar arm lại nhiều lần cùng một vùng."""
    if candidate.get('opportunity_id'):
        return str(candidate['opportunity_id'])
    return f"BTCUSDT:{candidate['zone_id']}:sv{int(structure_version)}"


def _opportunity_registry(state):
    registry = getattr(state, 'breakout_opportunities', None)
    if not isinstance(registry, dict):
        registry = {}
        state.breakout_opportunities = registry
    return registry


def _coalesce_breakout_opportunity(state, candidate, current, atr, now_wall, now_mono):
    """Map many M1 events onto one structural market opportunity."""
    base = breakout_policy.opportunity_base_key(
        state, candidate['bias'], candidate['zone'],
        getattr(state, 'trend_m15', ''),
    )
    registry = _opportunity_registry(state)
    opportunity = registry.get(base)
    terminal_reset = bool(
        opportunity
        and opportunity.get('state') in ('INVALIDATED', 'EXPIRED', 'EXECUTED')
        and now_mono >= float(opportunity.get('expires_mono', 0.0))
    )
    if opportunity is None or terminal_reset:
        sequence = int(getattr(state, 'breakout_opportunity_sequence', 0) or 0) + 1
        state.breakout_opportunity_sequence = sequence
        opportunity = {
            'opportunity_id': f"BTCUSDT:BREAKOUT:{base}:o{sequence}",
            'base_key': base,
            'direction': candidate['bias'],
            'level': float(candidate['zone']),
            'mode': candidate['mode'],
            'state': 'WAIT_RETEST',
            'created_at': now_wall,
            'created_mono': now_mono,
            'expires_mono': now_mono + BREAKOUT_OPPORTUNITY_TTL_SECONDS,
            'event_ids': [],
            'retest_since_mono': None,
            'advisory_only': bool(candidate.get('advisory_only', False)),
        }
        registry[base] = opportunity
        while len(registry) > BREAKOUT_OPPORTUNITY_LIMIT:
            registry.pop(next(iter(registry)))
    event_id = candidate.get('breakout_event_id')
    if event_id not in (None, '') and str(event_id) not in opportunity['event_ids']:
        opportunity['event_ids'].append(str(event_id))
        opportunity['event_ids'] = opportunity['event_ids'][-32:]
    opportunity['advisory_only'] = bool(
        opportunity.get('advisory_only', False)
        or candidate.get('advisory_only', False)
    )
    policy = breakout_policy.evaluate(
        state, current, candidate['bias'],
        float((getattr(state, 'exchange_filters', {}) or {}).get('tick_size', 0.1)),
    )
    opportunity.update(policy)
    candidate.update({
        'opportunity_id': opportunity['opportunity_id'],
        'opportunity_event_ids': list(opportunity['event_ids']),
        'entry_style': opportunity.get('entry_style'),
        'passive_entry_price': opportunity.get('passive_entry_price'),
        'breakout_target': policy['target'],
        'breakout_target2': policy['target2'],
        'breakout_target_basis': policy['target_basis'],
        'minimum_raw_target_bps': policy['minimum_raw_target_bps'],
    })
    return opportunity


def _active_opportunity_candidates(state, now_mono):
    candidates = []
    for opportunity in _opportunity_registry(state).values():
        if opportunity.get('state') in ('INVALIDATED', 'EXPIRED', 'EXECUTED'):
            continue
        if now_mono >= float(opportunity.get('expires_mono', 0.0)):
            opportunity['state'] = 'EXPIRED'
            continue
        candidates.append({
            'key': f"BREAKOUT-OPPORTUNITY:{opportunity['opportunity_id']}",
            'zone_id': f"{opportunity['mode']}:{opportunity['direction']}",
            'mode': opportunity['mode'],
            'bias': opportunity['direction'],
            'zone': opportunity['level'],
            'kind': 'breakout',
            'breakout_event_id': (
                opportunity.get('event_ids') or [None]
            )[-1],
            'opportunity_id': opportunity['opportunity_id'],
            'opportunity_event_ids': list(opportunity.get('event_ids', ())),
            'entry_style': opportunity.get('entry_style'),
            'passive_entry_price': opportunity.get('passive_entry_price'),
            'breakout_target': opportunity.get('target', 0.0),
            'breakout_target2': opportunity.get('target2', 0.0),
            'breakout_target_basis': opportunity.get('target_basis'),
            'minimum_raw_target_bps': opportunity.get('minimum_raw_target_bps'),
            'advisory_only': bool(opportunity.get('advisory_only', False)),
            # A 60-second setup is only a scoring window.  These fields belong
            # to the longer-lived structural opportunity and must survive a
            # setup roll, otherwise persistence restarts from zero precisely
            # while a valid retest is developing.
            'continuous_eligible_since_mono': opportunity.get(
                'continuous_eligible_since_mono'
            ),
            'max_continuous_score': float(
                opportunity.get('max_continuous_score', 0.0) or 0.0
            ),
            'peak_trade_power': float(
                opportunity.get('peak_trade_power', 0.0) or 0.0
            ),
        })
    return candidates


def _find_opportunity(state, opportunity_id):
    for opportunity in _opportunity_registry(state).values():
        if opportunity.get('opportunity_id') == opportunity_id:
            return opportunity
    return None


def _sync_breakout_setup_memory(state, setup):
    """Copy only causal scoring memory into its structural opportunity."""
    if setup.get('kind') != 'breakout':
        return
    opportunity = _find_opportunity(state, setup.get('opportunity_id'))
    if opportunity is None:
        return
    opportunity['max_continuous_score'] = max(
        float(opportunity.get('max_continuous_score', 0.0) or 0.0),
        float(setup.get('max_continuous_score', 0.0) or 0.0),
    )
    last_score = setup.get('last_score') or {}
    opportunity['peak_trade_power'] = max(
        float(opportunity.get('peak_trade_power', 0.0) or 0.0),
        float(last_score.get('trade_power', 0.0) or 0.0),
    )
    if 'continuous_eligible_since_mono' in setup:
        opportunity['continuous_eligible_since_mono'] = float(
            setup['continuous_eligible_since_mono']
        )
    else:
        # entry_ready() deliberately removes this when evidence decays.  Do
        # not resurrect stale persistence on the next setup generation.
        opportunity.pop('continuous_eligible_since_mono', None)


def _roll_retryable_breakout_setup(state, setup, prior_state, reason, now_wall):
    """Roll a short scoring window without killing the market opportunity."""
    if (
        not strategy_profile.aug13_early_hybrid_enabled()
        or setup.get('kind') != 'breakout'
        or prior_state not in ('WATCH', 'ARMED_WINDOW')
        or reason not in ('TTL', 'candidate removed', 'system not ready', 'strategy standby')
    ):
        return False
    opportunity = _find_opportunity(state, setup.get('opportunity_id'))
    if not opportunity or opportunity.get('state') in (
        'INVALIDATED', 'EXPIRED', 'EXECUTED'
    ):
        return False
    if time.monotonic() >= float(opportunity.get('expires_mono', 0.0)):
        return False
    _sync_breakout_setup_memory(state, setup)
    setup['state'] = 'ROLLED'
    if opportunity.get('state') != 'READY':
        opportunity['state'] = 'WAIT_RETEST'
    opportunity['last_setup_roll_reason'] = reason
    opportunity['last_setup_roll_at'] = float(now_wall)
    opportunity['setup_roll_count'] = int(
        opportunity.get('setup_roll_count', 0)
    ) + 1
    if hasattr(state, 'journal_events'):
        state.journal_events.append({
            'ts': float(now_wall),
            'event': 'BREAKOUT_SETUP_ROLLED',
            'run_id': getattr(state, 'run_id', None),
            'position_cycle_id': None,
            'payload': {
                'setup_id': setup.get('setup_id'),
                'opportunity_id': setup.get('opportunity_id'),
                'reason': reason,
                'setup_roll_count': opportunity['setup_roll_count'],
                'max_continuous_score': opportunity['max_continuous_score'],
                'peak_trade_power': opportunity['peak_trade_power'],
            },
        })
    return True


def _advance_breakout_opportunity(opportunity, current, atr, now_mono):
    """Require a level retest; chasing is only allowed with enough real edge."""
    direction = opportunity['direction']
    level = float(opportunity['level'])
    atr = max(float(atr or 0.0), 1e-9)
    adverse = level - current if direction == 'LONG' else current - level
    if now_mono >= float(opportunity['expires_mono']):
        opportunity['state'] = 'EXPIRED'
        return 'IDLE', 'OPPORTUNITY_EXPIRED'
    if adverse > BREAKOUT_FAILURE_ATR * atr:
        opportunity['state'] = 'INVALIDATED'
        return 'IDLE', 'BREAKOUT_FAILED'
    distance = abs(current - level)
    holding_side = current >= level if direction == 'LONG' else current <= level
    if distance <= BREAKOUT_RETEST_BAND_ATR * atr and holding_side:
        if opportunity.get('retest_since_mono') is None:
            opportunity['retest_since_mono'] = now_mono
            return 'WAIT_RETEST', 'RETEST_TOUCH'
        if now_mono - float(opportunity['retest_since_mono']) >= BREAKOUT_RETEST_HOLD_SECONDS:
            opportunity['state'] = 'READY'
            opportunity['entry_style'] = 'PASSIVE_RETEST'
            opportunity['passive_entry_price'] = level
            return 'FULL_ARM', 'RETEST_HELD'
        return 'WAIT_RETEST', 'RETEST_HOLD'
    opportunity['retest_since_mono'] = None
    age = now_mono - float(opportunity['created_mono'])
    if age >= BREAKOUT_CHASE_WAIT_SECONDS and opportunity.get('market_chase_allowed'):
        opportunity['state'] = 'READY'
        opportunity['entry_style'] = 'MARKET_CHASE'
        return 'FULL_ARM', 'CHASE_HAS_MEANINGFUL_TARGET'
    return 'WAIT_RETEST', 'WAITING_RETEST'


def _rearm_block_active(state, semantic_key, current, zone, atr):
    block = getattr(state, 'rearm_blocks', {}).get(semantic_key)
    if not block:
        return False
    frozen_zone = float(block.get('zone', 0.0) or zone)
    if _in_retention_zone(current, frozen_zone, atr):
        return True
    state.rearm_blocks.pop(semantic_key, None)
    return False


def _intent_terminal_blocked(state, opportunity_id, structure_version):
    registry = getattr(state, 'intent_terminal_opportunities', None)
    if not isinstance(registry, dict):
        return False
    record = registry.get(str(opportunity_id))
    if not record:
        return False
    if int(record.get('structure_version', -1)) != int(structure_version):
        registry.pop(str(opportunity_id), None)
        return False
    return True


def _breakout_attempt_tombstones(state):
    tombstones = getattr(state, 'attempted_breakout_events', None)
    if not isinstance(tombstones, dict):
        tombstones = {}
        state.attempted_breakout_events = tombstones
    return tombstones


def _breakout_attempt_blocked(state, candidate):
    """Only a breakout event is one-shot; price-zone candidates stay untouched."""
    if candidate.get('kind') != 'breakout':
        return False
    event_id = candidate.get('breakout_event_id')
    if event_id in (None, ''):
        return False
    return str(event_id) in _breakout_attempt_tombstones(state)


def _remember_terminal_breakout_attempt(
    state, setup, prior_state, reason, now_wall,
):
    """Tombstone a claimed breakout only after a failed terminal transition."""
    if setup.get('kind') != 'breakout' or not setup.get('claimed_once'):
        return
    if prior_state == 'EXECUTED' or setup.get('state') not in (
        'INVALIDATED', 'EXPIRED',
    ):
        return
    opportunity = _find_opportunity(state, setup.get('opportunity_id'))
    if opportunity is not None:
        opportunity['state'] = 'INVALIDATED'
        opportunity['terminal_reason'] = reason

    event_id = setup.get('breakout_event_id')
    if event_id in (None, ''):
        return

    tombstones = _breakout_attempt_tombstones(state)
    event_id = str(event_id)
    tombstones[event_id] = {
        'setup_id': setup.get('setup_id'),
        'terminal_state': setup.get('state'),
        'reason': reason,
        'recorded_at': float(now_wall),
    }
    while len(tombstones) > BREAKOUT_EVENT_TOMBSTONE_LIMIT:
        tombstones.pop(next(iter(tombstones)))


def _new_setup(state, candidate, arm_sequence, now_mono):
    state.setup_generation = getattr(state, 'setup_generation', 0) + 1
    generation = state.setup_generation
    structure_version = int(getattr(state, 'structure_version', 0))
    semantic_key = semantic_setup_key(candidate, structure_version)
    setup_id = (
        f"BTCUSDT:{candidate['zone_id']}:sv{structure_version}:a{arm_sequence}"
    )
    armed_price = (
        float(getattr(state, 'best_ask', 0.0) or 0.0)
        if candidate['bias'] == 'LONG'
        else float(getattr(state, 'best_bid', 0.0) or 0.0)
    )
    setup_ttl = (
        NEUTRAL_MOMENTUM_TTL_SECONDS
        if candidate.get('mode') == 'NEUTRAL-MOMENTUM'
        else ARMED_TTL_SECONDS
    )
    setup = {
        'setup_id': setup_id,
        'semantic_key': semantic_key,
        'generation': generation,
        'structure_version': structure_version,
        'arm_sequence': arm_sequence,
        'zone_id': candidate['zone_id'],
        'mode': candidate['mode'],
        'bias': candidate['bias'],
        'zone': candidate['zone'],
        'kind': candidate['kind'],
        'activation_reason': candidate.get('activation_reason'),
        'breakout_event_id': candidate.get('breakout_event_id'),
        'opportunity_id': candidate.get('opportunity_id') or semantic_key,
        'opportunity_event_ids': list(candidate.get('opportunity_event_ids', ())),
        'entry_style': candidate.get('entry_style'),
        'passive_entry_price': candidate.get('passive_entry_price'),
        'value_migration_retest': bool(
            candidate.get('value_migration_retest', False)
        ),
        'value_boundary': candidate.get('value_boundary'),
        'location_role': candidate.get('location_role'),
        'breakout_target': candidate.get('breakout_target', 0.0),
        'breakout_target2': candidate.get('breakout_target2', 0.0),
        'breakout_target_basis': candidate.get('breakout_target_basis'),
        'minimum_raw_target_bps': candidate.get('minimum_raw_target_bps'),
        'advisory_only': bool(candidate.get('advisory_only', False)),
        'shadow_qualified_once': False,
        'claimed_once': False,
        'state': _watch_state(),
        'created_at': time.time(),
        'created_mono': now_mono,
        'armed_price': armed_price,
        'expires_mono': now_mono + setup_ttl,
        'opportunity_ttl_seconds': setup_ttl,
        'last_score_mono': 0.0,
        'last_revision': -1,
        'last_score': None,
        'score_count': 0,
        'evaluation_count': 0,
        'core_reject_count': 0,
        'veto_count': 0,
        'max_core': 0,
        'max_shark': 0,
        'last_veto': None,
        'best_score': None,
        'seen_score_details': [],
    }
    if candidate.get('continuous_eligible_since_mono') is not None:
        setup['continuous_eligible_since_mono'] = float(
            candidate['continuous_eligible_since_mono']
        )
    setup['max_continuous_score'] = float(
        candidate.get('max_continuous_score', 0.0) or 0.0
    )
    setup['peak_trade_power'] = float(
        candidate.get('peak_trade_power', 0.0) or 0.0
    )
    return setup


def _record_setup_outcome(state, setup, prior_state, reason, reference_price, now_wall):
    """Ghi funnel và mở counterfactual; không tác động quyết định entry."""
    if prior_state in ('EXECUTING', 'EXECUTED'):
        return
    reference_price = float(reference_price or setup.get('armed_price', 0.0) or 0.0)
    outcome = {
        'setup_id': setup.get('setup_id'),
        'setup_ids': [setup.get('setup_id')],
        'opportunity_id': _continuous_opportunity_key(setup),
        'generation': int(setup.get('generation', 0)),
        'mode': setup.get('mode'),
        'bias': setup.get('bias'),
        'zone': float(setup.get('zone', 0.0) or 0.0),
        'armed_price': float(setup.get('armed_price', 0.0) or 0.0),
        'reference_price': reference_price,
        'created_at': float(setup.get('created_at', now_wall) or now_wall),
        'ended_at': now_wall,
        'terminal_state': setup.get('state'),
        'reason': reason,
        'age_seconds': max(
            0.0, time.monotonic() - float(setup.get('created_mono', 0.0))
        ),
        'score_count': int(setup.get('score_count', 0)),
        'evaluation_count': int(setup.get('evaluation_count', 0)),
        'core_reject_count': int(setup.get('core_reject_count', 0)),
        'veto_count': int(setup.get('veto_count', 0)),
        'last_veto': setup.get('last_veto'),
        'max_core': int(setup.get('max_core', 0)),
        'max_shark': int(setup.get('max_shark', 0)),
        'best_score': setup.get('best_score'),
        'seen_score_details': list(setup.get('seen_score_details', [])),
        'followup': {
            'started_at': now_wall,
            'expires_at': now_wall + SETUP_FOLLOWUP_SECONDS,
            'estimated_round_trip_cost_bps': SETUP_FOLLOWUP_COST_BPS,
            'mfe_bps': 0.0,
            'mae_bps': 0.0,
            'checkpoints': {},
            'completed': False,
        },
    }
    existing = next((
        item for item in getattr(state, 'setup_outcomes', ())
        if item.get('opportunity_id') == outcome['opportunity_id']
    ), None)
    if existing is not None:
        existing['ended_at'] = now_wall
        existing['terminal_state'] = outcome['terminal_state']
        existing['reason'] = reason
        existing['latest_setup_id'] = outcome['setup_id']
        setup_ids = list(existing.get('setup_ids', ()))
        if outcome['setup_id'] not in setup_ids:
            setup_ids.append(outcome['setup_id'])
        existing['setup_ids'] = setup_ids[-32:]
        existing['score_count'] = int(existing.get('score_count', 0)) + outcome['score_count']
        existing['evaluation_count'] = int(
            existing.get('evaluation_count', 0)
        ) + outcome['evaluation_count']
        existing['core_reject_count'] = int(
            existing.get('core_reject_count', 0)
        ) + outcome['core_reject_count']
        existing['veto_count'] = int(existing.get('veto_count', 0)) + outcome['veto_count']
        existing['max_core'] = max(int(existing.get('max_core', 0)), outcome['max_core'])
        existing['max_shark'] = max(int(existing.get('max_shark', 0)), outcome['max_shark'])
        existing['best_score'] = max(
            (value for value in (existing.get('best_score'), outcome['best_score'])
             if isinstance(value, (int, float))),
            default=existing.get('best_score') or outcome['best_score'],
        )
        _finalize_continuous_shadow(state, setup, existing, now_wall)
        if hasattr(state, 'journal_events'):
            state.journal_events.append({
                'ts': now_wall, 'event': 'SETUP_OUTCOME_COALESCED',
                'position_cycle_id': None,
                'payload': {
                    'opportunity_id': outcome['opportunity_id'],
                    'setup_id': outcome['setup_id'], 'reason': reason,
                    'sample_count_increment': 0,
                },
            })
        return
    state.setup_outcomes.append(outcome)
    state.setup_followups.append(outcome)
    _finalize_continuous_shadow(state, setup, outcome, now_wall)
    if hasattr(state, 'journal_events'):
        state.journal_events.append({
            'ts': now_wall,
            'event': 'SETUP_OUTCOME_STARTED',
            'position_cycle_id': None,
            'payload': {
                'setup_id': outcome['setup_id'],
                'mode': outcome['mode'],
                'bias': outcome['bias'],
                'reason': reason,
                'max_core': outcome['max_core'],
                'max_shark': outcome['max_shark'],
                'evaluation_count': outcome['evaluation_count'],
                'score_count': outcome['score_count'],
                'core_reject_count': outcome['core_reject_count'],
                'veto_count': outcome['veto_count'],
                'last_veto': outcome['last_veto'],
                'best_score': outcome['best_score'],
                'reference_price': outcome['reference_price'],
            },
        })


def _update_setup_followups(state, current_price, now_wall):
    """Theo dõi no-lookahead MFE/MAE của setup bị bỏ qua tới 45 phút."""
    current_price = float(current_price or 0.0)
    if current_price <= 0.0:
        return
    for outcome in getattr(state, 'setup_followups', ()):
        followup = outcome.get('followup', {})
        if followup.get('completed'):
            continue
        reference = float(outcome.get('reference_price', 0.0) or 0.0)
        if reference <= 0.0:
            followup['completed'] = True
            followup['completion_reason'] = 'INVALID_REFERENCE_PRICE'
            continue
        raw_move_bps = (current_price - reference) / reference * 10000.0
        directional_bps = (
            raw_move_bps if outcome.get('bias') == 'LONG' else -raw_move_bps
        )
        followup['mfe_bps'] = max(
            float(followup.get('mfe_bps', 0.0)), directional_bps
        )
        followup['mae_bps'] = min(
            float(followup.get('mae_bps', 0.0)), directional_bps
        )
        elapsed = max(0.0, now_wall - float(followup.get('started_at', now_wall)))
        checkpoints = followup.setdefault('checkpoints', {})
        for seconds in SETUP_FOLLOWUP_CHECKPOINTS:
            key = str(seconds)
            if elapsed >= seconds and key not in checkpoints:
                checkpoints[key] = {
                    'move_bps': directional_bps,
                    'mfe_bps': followup['mfe_bps'],
                    'mae_bps': followup['mae_bps'],
                    'net_move_bps': (
                        directional_bps - SETUP_FOLLOWUP_COST_BPS
                    ),
                    'net_mfe_bps': (
                        followup['mfe_bps'] - SETUP_FOLLOWUP_COST_BPS
                    ),
                }
        if now_wall >= float(followup.get('expires_at', 0.0)):
            followup['completed'] = True
            followup['completed_at'] = now_wall


def _invalidate(state, setups, key, reason, reference_price=0.0, now_wall=None):
    setup = setups.pop(key, None)
    if setup is None:
        return
    prior_state = setup.get('state')
    now_wall = time.time() if now_wall is None else float(now_wall)
    if _roll_retryable_breakout_setup(
        state, setup, prior_state, reason, now_wall
    ):
        age = max(
            0.0, time.monotonic() - float(setup.get('created_mono', 0.0))
        )
        logging.info(
            "🔄 [RADAR] ROLLED %s: %s | opportunity=%s age=%.2fs "
            "peak_power=%.2f",
            setup.get('setup_id'), reason, setup.get('opportunity_id'), age,
            float(setup.get('peak_trade_power', 0.0) or 0.0),
        )
        return
    setup['state'] = 'INVALIDATED' if reason != 'TTL' else 'EXPIRED'
    if prior_state in ('EXECUTING', 'EXECUTED'):
        registry = getattr(state, 'setup_terminal_by_identity', None)
        if not isinstance(registry, dict):
            registry = {}
            state.setup_terminal_by_identity = registry
        identity = (
            str(setup.get('setup_id') or ''),
            int(setup.get('generation', 0) or 0),
        )
        registry[identity] = {
            'state': setup['state'], 'reason': reason, 'ts': now_wall,
            'opportunity_id': setup.get('opportunity_id'),
            'semantic_key': setup.get('semantic_key'),
        }
        while len(registry) > 512:
            oldest = min(
                registry,
                key=lambda item: float(registry[item].get('ts', 0.0) or 0.0),
            )
            registry.pop(oldest, None)
    if setup.get('kind') == 'breakout' and prior_state != 'EXECUTED':
        opportunity = _find_opportunity(state, setup.get('opportunity_id'))
        if opportunity is not None:
            opportunity['state'] = setup['state']
            opportunity['terminal_reason'] = reason
    _remember_terminal_breakout_attempt(
        state, setup, prior_state, reason, now_wall
    )
    if prior_state in ('EXECUTING', 'EXECUTED'):
        _finalize_continuous_shadow(state, setup, {
            'setup_id': setup.get('setup_id'),
            'terminal_state': prior_state,
            'reason': reason,
        }, now_wall)
    _record_setup_outcome(
        state, setup, prior_state, reason, reference_price, now_wall
    )
    age = max(0.0, time.monotonic() - float(setup.get('created_mono', 0.0)))
    logging.info(
        "🧹 [RADAR] %s %s: %s | age=%.2fs scores=%s "
        "max_core=%s max_shark=%s core_rejects=%s vetoes=%s last_veto=%s",
        setup['state'], setup['setup_id'], reason, age,
        setup.get('score_count', 0), setup.get('max_core', 0),
        setup.get('max_shark', 0), setup.get('core_reject_count', 0),
        setup.get('veto_count', 0), setup.get('last_veto'),
    )


def _setup_is_current(setup, candidate, state, current, now_wall, now_mono):
    if setup.get('state') not in ('WATCH', 'ARMED_WINDOW', 'EXECUTING'):
        return False, 'terminal'
    if int(setup.get('structure_version', -1)) != int(getattr(state, 'structure_version', 0)):
        return False, 'structure changed'
    if setup.get('mode') != candidate['mode'] or setup.get('bias') != candidate['bias']:
        return False, 'mode/bias changed'
    if now_mono >= float(setup.get('expires_mono', 0.0)):
        return False, 'TTL'
    if candidate['kind'] == 'breakout':
        opportunity = _find_opportunity(state, setup.get('opportunity_id'))
        valid = bool(
            opportunity
            and opportunity.get('state') not in ('INVALIDATED', 'EXPIRED')
            and opportunity.get('opportunity_id') == candidate.get('opportunity_id')
        )
        return valid, 'breakout opportunity terminal'
    if _opposing_breakout(state, candidate['bias'], now_wall):
        return False, 'opposing breakout'
    atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
    if abs(float(setup['zone']) - float(candidate['zone'])) > PRE_ARM_ZONE_MOVE_MULT * atr:
        return False, 'zone moved'
    if setup.get('mode') == 'NEUTRAL-MOMENTUM' and _continuous_v2_enabled():
        return True, 'neutral momentum watch active'
    return _in_retention_zone(current, setup['zone'], atr), 'left RETENTION zone'


async def vong_lap_radar(state):
    scorer_version = str(os.getenv('SMC_SCORER_VERSION', 'CORE_V1') or 'CORE_V1').upper()
    active_continuous_scorer = (
        continuous_scorer_v2
        if scorer_version == continuous_scorer_v2.LIVE_VERSION
        else continuous_scorer
    )
    continuous_live = scorer_version == active_continuous_scorer.LIVE_VERSION
    continuous_shadow_enabled = _continuous_shadow_enabled()
    if not continuous_shadow_enabled:
        # Old snapshots may restore a full queue even though persistence is
        # disabled.  Clearing it once prevents permanent drop-counter churn.
        shadow_queue = getattr(state, 'continuous_shadow_events', None)
        if shadow_queue is not None:
            shadow_queue.clear()
        state.continuous_shadow_drop_count = 0
    requested_ml_mode = ml_meta_artifact.requested_mode()
    ml_artifact, ml_artifact_status = ml_meta_artifact.load_artifact()
    ml_authority = ml_meta_artifact.authority(requested_ml_mode, ml_artifact)
    state.ml_meta_mode = ml_authority['mode']
    state.ml_meta_artifact_status = ml_artifact_status
    logging.info(
        "📡 [RADAR] %s + event coalescing đã khởi động | scorer=%s shadow=%s",
        _watch_state(), scorer_version,
        'CONTINUOUS_SHADOW_V1' if continuous_shadow_enabled else 'OFF',
    )
    logging.info(
        "🧪 [ML META] requested=%s effective=%s artifact=%s live_authority=false",
        requested_ml_mode, ml_authority['mode'], ml_artifact_status,
    )
    _queue_ml_meta(state, {
        'event_type': 'POLICY_ACTIVE',
        'schema_version': ml_meta_scout.SCHEMA_VERSION,
        'policy_version': ml_meta_scout.POLICY_VERSION,
        'requested_mode': requested_ml_mode,
        'effective_mode': ml_authority['mode'],
        'artifact_status': ml_artifact_status,
        'live_authority': False,
    })
    _emit_radar_event(state, 'SCORER_POLICY_ACTIVE', {
        'score_version': scorer_version,
        'watch_state': _watch_state(),
        'maximum_size_pct': 9.0 if continuous_live else None,
        'rollback_flag': 'SMC_SCORER_VERSION=CONTINUOUS_V1',
        'rollback_backup': '/home/ubuntu/SMC2026(26)-',
        'reference_scorer': 'CONTINUOUS_V1' if scorer_version == 'CONTINUOUS_V2' else None,
    })
    if continuous_shadow_enabled:
        _queue_continuous_shadow(state, {
            'analysis_type': 'POLICY_ACTIVE',
            'version': 'CONTINUOUS_SHADOW_V1',
            'live_scorer': scorer_version,
            'live_authority': False,
        })
    while getattr(state, "best_bid", 0.0) <= 0.0:
        await asyncio.sleep(0.1)

    last_ask = state.best_ask
    last_bid = state.best_bid
    arm_states = {}
    arm_probes = {}
    setups = {}
    arm_sequences = {}
    blocked_log_last = {}

    while True:
        now_wall = time.time()
        now_mono = time.monotonic()
        current_ask = state.best_ask
        current_bid = state.best_bid
        current_mid = (
            (current_bid + current_ask) / 2.0
            if current_bid > 0.0 and current_ask > 0.0
            else max(current_bid, current_ask)
        )
        _update_setup_followups(state, current_mid, now_wall)
        mode_info = getattr(state, "current_mode", {"modes": ["STANDBY"]})
        reversal_context.update(state, now_wall)
        trend_context.update(state, now_wall)
        try:
            _collect_ml_meta_scout(
                state, active_continuous_scorer, mode_info, now_wall, now_mono
            )
        except Exception as ml_exc:
            state.ml_meta_health_errors = int(
                getattr(state, 'ml_meta_health_errors', 0)
            ) + 1
            if state.ml_meta_health_errors <= 3 or state.ml_meta_health_errors % 60 == 0:
                logging.exception(
                    "❌ [ML META] collect lỗi; live baseline không bị ảnh hưởng: %s",
                    ml_exc,
                )

        fresh_breakout = _fresh_breakout_event(state, now_wall)
        active_breakouts = _active_opportunity_candidates(state, now_mono)
        if (
            not getattr(state, "system_ready", False)
            or (
                "STANDBY" in mode_info.get("modes", [])
                and fresh_breakout is None and not active_breakouts
            )
        ):
            for key in list(setups):
                if setups[key].get('state') != 'EXECUTING':
                    reason = (
                        'system not ready'
                        if not getattr(state, 'system_ready', False)
                        else 'strategy standby'
                    )
                    _invalidate(state, setups, key, reason, current_mid, now_wall)
            arm_states.clear()
            arm_probes.clear()
            state.active_setups = dict(setups)
            state.arm_state = "EXECUTING" if setups else "IDLE"
            last_ask, last_bid = current_ask, current_bid
            await asyncio.sleep(RADAR_LOOP_INTERVAL)
            continue

        atr = float(getattr(state, "atr_1m", 0.0) or 0.0)
        candidates = build_candidates(mode_info, fresh_breakout)
        # First fold every fresh M1 breakout into a structural opportunity,
        # then expose exactly one candidate per opportunity. Continuation,
        # retest and reclaim candles update the same lifecycle.
        non_breakouts = [item for item in candidates if item['kind'] != 'breakout']
        for item in (candidate for candidate in candidates if candidate['kind'] == 'breakout'):
            price = current_ask if item['bias'] == 'LONG' else current_bid
            _coalesce_breakout_opportunity(
                state, item, price, atr, now_wall, now_mono
            )
        candidates = non_breakouts + _active_opportunity_candidates(state, now_mono)
        candidate_keys = {candidate['key'] for candidate in candidates}
        for key in list(setups):
            if key not in candidate_keys:
                _invalidate(
                    state, setups, key, 'candidate removed', current_mid, now_wall
                )
        for key in list(arm_probes):
            if key not in candidate_keys:
                arm_probes.pop(key, None)

        new_states = {}
        diagnostics = {}
        for candidate in candidates:
            key = candidate['key']
            current = current_ask if candidate['bias'] == 'LONG' else current_bid
            previous = last_ask if candidate['bias'] == 'LONG' else last_bid
            setup = setups.get(key)
            semantic_key = semantic_setup_key(
                candidate, int(getattr(state, 'structure_version', 0))
            )

            if setup is not None:
                valid, reason = _setup_is_current(
                    setup, candidate, state, current, now_wall, now_mono
                )
                if not valid:
                    _invalidate(state, setups, key, reason, current, now_wall)
                    setup = None

            if (
                setup is None
                and candidate['kind'] == 'zone'
                and _rearm_block_active(
                    state, semantic_key, current, candidate['zone'], atr
                )
            ):
                arm_probes.pop(key, None)
                new_states[key] = 'REARM_BLOCKED'
                diagnostics[key] = {
                    'state': 'REARM_BLOCKED',
                    'reason': 'MUST_LEAVE_RETENTION_AFTER_EXECUTION',
                    'price': float(current),
                    'zone': float(candidate['zone']),
                    'live_zone': float(candidate['zone']),
                    'atr': atr,
                    'distance': float(current) - float(candidate['zone']),
                    'distance_atr': (
                        (float(current) - float(candidate['zone'])) / atr
                        if atr > 0.0 else None
                    ),
                }
                continue

            if (
                setup is None
                and _intent_terminal_blocked(
                    state, semantic_key,
                    int(getattr(state, 'structure_version', 0)),
                )
            ):
                arm_probes.pop(key, None)
                new_states[key] = 'INTENT_TERMINAL'
                terminal = getattr(
                    state, 'intent_terminal_opportunities', {}
                ).get(str(semantic_key), {})
                diagnostics[key] = {
                    'state': 'INTENT_TERMINAL',
                    'reason': terminal.get('reason', 'ONE_INTENT_PER_OPPORTUNITY'),
                    'price': float(current), 'zone': float(candidate['zone']),
                    'live_zone': float(candidate['zone']), 'atr': atr,
                    'distance': float(current) - float(candidate['zone']),
                    'distance_atr': (
                        (float(current) - float(candidate['zone'])) / atr
                        if atr > 0.0 else None
                    ),
                }
                continue

            if setup is None and _breakout_attempt_blocked(state, candidate):
                event_id = candidate.get('breakout_event_id')
                new_states[key] = 'EVENT_ATTEMPTED'
                diagnostics[key] = {
                    'state': 'EVENT_ATTEMPTED',
                    'reason': 'BREAKOUT_EVENT_ALREADY_ATTEMPTED',
                    'breakout_event_id': event_id,
                    'price': float(current),
                    'zone': float(candidate['zone']),
                    'live_zone': float(candidate['zone']),
                    'atr': atr,
                    'distance': float(current) - float(candidate['zone']),
                    'distance_atr': (
                        (float(current) - float(candidate['zone'])) / atr
                        if atr > 0.0 else None
                    ),
                }
                continue

            activation_probe = None
            activation_reason = 'SETUP_ACTIVE' if setup is not None else 'IDLE'
            setup_candidate = candidate
            if setup is not None:
                raw_state = setup.get('state', _watch_state())
            elif candidate['kind'] == 'breakout':
                opportunity = _find_opportunity(
                    state, candidate.get('opportunity_id')
                )
                if opportunity is None:
                    raw_state, activation_reason = 'IDLE', 'OPPORTUNITY_MISSING'
                else:
                    # Re-evaluate the liquidity ladder at the executable price;
                    # ATR is never introduced as an economic target here.
                    policy = breakout_policy.evaluate(
                        state, current, candidate['bias'],
                        float((getattr(state, 'exchange_filters', {}) or {}).get('tick_size', 0.1)),
                    )
                    opportunity.update(policy)
                    candidate.update({
                        'entry_style': opportunity.get('entry_style'),
                        'passive_entry_price': opportunity.get('passive_entry_price'),
                        'breakout_target': policy['target'],
                        'breakout_target2': policy['target2'],
                        'breakout_target_basis': policy['target_basis'],
                        'minimum_raw_target_bps': policy['minimum_raw_target_bps'],
                    })
                    raw_state, activation_reason = _advance_breakout_opportunity(
                        opportunity, current, atr, now_mono
                    )
                    if _continuous_v2_enabled() and raw_state == 'WAIT_RETEST':
                        # V2 keeps the opportunity scoreable while distance
                        # decays smoothly through retest_fit. It still cannot
                        # chase: any eventual claim is maker/passive only.
                        opportunity['entry_style'] = 'PASSIVE_RETEST'
                        opportunity['passive_entry_price'] = float(opportunity['level'])
                        raw_state = 'FULL_ARM'
                        activation_reason = 'CONTINUOUS_RETEST_WATCH'
                    candidate['entry_style'] = opportunity.get('entry_style')
                    candidate['passive_entry_price'] = opportunity.get('passive_entry_price')
            else:
                if candidate['mode'] == 'NEUTRAL-MOMENTUM' and _continuous_v2_enabled():
                    raw_state = 'FULL_ARM'
                    activation_reason = 'NEUTRAL_MOMENTUM_WATCH'
                    setup_candidate = dict(candidate)
                    setup_candidate['activation_reason'] = activation_reason
                    activation_probe = None
                else:
                    previous_probe = arm_probes.get(key)
                    raw_state, activation_probe, activation_reason = advance_arm_probe(
                        current, previous, candidate['zone'], atr, candidate['bias'],
                        probe=previous_probe, now_mono=now_mono,
                    )
                previous_probe = arm_probes.get(key)
                if activation_probe is None:
                    arm_probes.pop(key, None)
                else:
                    arm_probes[key] = activation_probe
                if previous_probe is None and activation_probe is not None:
                    logging.info(
                        "👀 [RADAR] PRE-ARM %s %s price=%.2f zone=%.2f "
                        "atr=%.2f distance=%.2f reason=%s",
                        candidate['mode'], candidate['bias'], current,
                        activation_probe['zone'], activation_probe['atr'],
                        current - activation_probe['zone'], activation_reason,
                    )
                    _emit_radar_event(state, 'RADAR_PRE_ARM', {
                        'candidate_key': key,
                        'mode': candidate['mode'],
                        'bias': candidate['bias'],
                        'price': float(current),
                        'zone': float(activation_probe['zone']),
                        'live_zone': float(candidate['zone']),
                        'atr': float(activation_probe['atr']),
                        'reason': activation_reason,
                    })
                elif previous_probe is not None and activation_probe is None:
                    logging.info(
                        "↩️ [RADAR] PRE-ARM CANCEL %s %s price=%.2f "
                        "zone=%.2f reason=%s",
                        candidate['mode'], candidate['bias'], current,
                        previous_probe['zone'], activation_reason,
                    )
                    _emit_radar_event(state, 'RADAR_PRE_ARM_CANCEL', {
                        'candidate_key': key,
                        'mode': candidate['mode'],
                        'bias': candidate['bias'],
                        'price': float(current),
                        'zone': float(previous_probe['zone']),
                        'live_zone': float(candidate['zone']),
                        'atr': float(previous_probe['atr']),
                        'reason': activation_reason,
                    })
                elif activation_reason == 'ZONE_MOVED_RESTART':
                    logging.info(
                        "🔄 [RADAR] PRE-ARM RESTART %s %s live_zone=%.2f "
                        "frozen_zone=%.2f",
                        candidate['mode'], candidate['bias'], candidate['zone'],
                        activation_probe['zone'],
                    )
                elif (
                    activation_reason == 'SWEEP_WAIT_STARTED'
                    and activation_probe is not None
                    and not activation_probe.get('ttl_event_emitted')
                ):
                    activation_probe['ttl_event_emitted'] = True
                    logging.info(
                        "🧭 [RADAR] SWEEP-WAIT %s %s; chờ quét vùng rồi "
                        "reclaim có giữ giá.",
                        candidate['mode'], candidate['bias'],
                    )
                    _emit_radar_event(state, 'RADAR_SWEEP_WAIT', {
                        'candidate_key': key,
                        'mode': candidate['mode'],
                        'bias': candidate['bias'],
                        'price': float(current),
                        'zone': float(activation_probe['zone']),
                        'atr': float(activation_probe['atr']),
                        'reason': activation_reason,
                    })
                elif (
                    activation_reason == 'SWEEP_DETECTED'
                    and activation_probe is not None
                    and not activation_probe.get('sweep_event_emitted')
                ):
                    activation_probe['sweep_event_emitted'] = True
                    _emit_radar_event(state, 'RADAR_SWEEP_DETECTED', {
                        'candidate_key': key,
                        'mode': candidate['mode'],
                        'bias': candidate['bias'],
                        'price': float(current),
                        'zone': float(activation_probe['zone']),
                        'atr': float(activation_probe['atr']),
                        'penetration_atr': float(
                            activation_probe.get('max_penetration_atr', 0.0)
                        ),
                        'reason': activation_reason,
                    })
                if raw_state == 'FULL_ARM' and activation_probe is not None:
                    setup_candidate = dict(candidate)
                    setup_candidate['zone'] = float(activation_probe['zone'])
                    setup_candidate['activation_reason'] = activation_reason

            if (
                setup is None
                and raw_state == 'FULL_ARM'
                and candidate['kind'] == 'zone'
                and _opposing_breakout(state, candidate['bias'], now_wall)
            ):
                log_key = f"{candidate['mode']}:{candidate['bias']}"
                if now_mono - float(blocked_log_last.get(log_key, 0.0)) >= 5.0:
                    blocked_log_last[log_key] = now_mono
                    logging.info(
                        "🧱 [RADAR] BLOCKED %s %s: opposing breakout",
                        candidate['mode'], candidate['bias'],
                    )
                raw_state = 'IDLE'

            if setup is None and raw_state == 'FULL_ARM':
                arm_sequences[key] = arm_sequences.get(key, 0) + 1
                setup = _new_setup(
                    state, setup_candidate, arm_sequences[key], now_mono
                )
                setups[key] = setup
                arm_probes.pop(key, None)
                logging.info(
                    "🎯 [RADAR] %s %s %s setup=%s price=%.2f "
                    "zone=%.2f reason=%s",
                    setup['state'], candidate['mode'], candidate['bias'], setup['setup_id'],
                    current, setup['zone'], activation_reason,
                )
                _emit_radar_event(state, 'RADAR_WATCH' if setup['state'] == 'WATCH' else 'RADAR_ARMED_WINDOW', {
                    'setup_id': setup['setup_id'],
                    'generation': int(setup['generation']),
                    'candidate_key': key,
                    'mode': candidate['mode'],
                    'bias': candidate['bias'],
                    'price': float(current),
                    'zone': float(setup['zone']),
                    'live_zone': float(candidate['zone']),
                    'atr': float(atr),
                    'reason': activation_reason,
                    'breakout_event_id': setup.get('breakout_event_id'),
                    'opportunity_id': setup.get('opportunity_id'),
                    'entry_style': setup.get('entry_style'),
                    'breakout_target_basis': setup.get('breakout_target_basis'),
                })

            passive_pending = bool(
                setup is not None
                and setup.get('state') == 'EXECUTING'
                and setup.get('passive_intent_active')
            )
            if setup is not None and (
                setup.get('state') in ('WATCH', 'ARMED_WINDOW') or passive_pending
            ):
                revision = int(getattr(state, 'decision_revision', 0))
                since_score = now_mono - float(setup.get('last_score_mono', 0.0))
                dirty = revision != int(setup.get('last_revision', -1))
                live_should_score = (
                    (dirty and since_score >= RESCORE_MIN_INTERVAL)
                    or since_score >= RESCORE_FALLBACK_INTERVAL
                )
                opportunity_id = _continuous_opportunity_key(setup)
                shadow_schedules = getattr(state, 'continuous_shadow_schedule', None)
                if not isinstance(shadow_schedules, dict):
                    shadow_schedules = {}
                    state.continuous_shadow_schedule = shadow_schedules
                shadow_schedule = shadow_schedules.setdefault(
                    opportunity_id, {
                        'last_revision': -1, 'last_score_mono': 0.0,
                        'last_error_emit_mono': 0.0,
                    },
                )
                continuous_revision = int(
                    getattr(state, 'continuous_evidence_revision', 0)
                )
                shadow_since = now_mono - float(
                    shadow_schedule.get('last_score_mono', 0.0)
                )
                shadow_dirty = continuous_revision != int(
                    shadow_schedule.get('last_revision', -1)
                )
                shadow_should_score = bool(
                    continuous_shadow_enabled and (
                        (shadow_dirty and shadow_since >= RESCORE_MIN_INTERVAL)
                        or shadow_since >= RESCORE_FALLBACK_INTERVAL
                    )
                )
                if live_should_score or shadow_should_score:
                    decision = snapshot_mod.capture(state, setup, now_wall, now_mono)
                    continuous_result = None
                    continuous_should_score = bool(
                        shadow_should_score or (continuous_live and live_should_score)
                    )
                    if continuous_should_score:
                        shadow_schedule['last_revision'] = continuous_revision
                        shadow_schedule['last_score_mono'] = now_mono
                        try:
                            continuous_result = active_continuous_scorer.score_continuous(
                                decision, setup, mode_info, live=continuous_live
                            )
                            if passive_pending and continuous_live:
                                setup['passive_live_score'] = dict(continuous_result)
                                setup['passive_live_score_mono'] = now_mono
                            if continuous_shadow_enabled:
                                _record_continuous_shadow_score(
                                    state, setup, decision, continuous_result,
                                    now_wall, now_mono,
                                )
                            side_mode = str(
                                os.getenv('SMC_SIDE_CALIBRATION_MODE', 'SHADOW')
                            ).upper()
                            side_due = bool(
                                side_mode == 'BOUNDED_LIVE'
                                or now_mono - float(
                                    shadow_schedule.get('last_side_score_mono', 0.0)
                                ) >= SIDE_SHADOW_SCORE_INTERVAL
                            )
                            if _side_calibration_shadow_enabled() and side_due:
                                try:
                                    shadow_schedule['last_side_score_mono'] = now_mono
                                    side_result = side_scorer_v21.score_side_calibrated(
                                        decision, setup, mode_info
                                    )
                                    state.side_calibration_version = side_result.get(
                                        'calibration_version'
                                    )
                                    state.side_calibration_hash = side_result.get(
                                        'calibration_hash'
                                    )
                                    _record_side_calibration_shadow(
                                        state, setup, side_result, now_wall
                                    )
                                    if side_result.get('bounded_live_applied'):
                                        continuous_result = (
                                            side_scorer_v21.apply_bounded_live(
                                                continuous_result, side_result
                                            )
                                        )
                                        if passive_pending and continuous_live:
                                            setup['passive_live_score'] = dict(
                                                continuous_result
                                            )
                                            setup['passive_live_score_mono'] = now_mono
                                except Exception as side_exc:
                                    state.continuous_shadow_health_errors = int(
                                        getattr(state, 'continuous_shadow_health_errors', 0)
                                    ) + 1
                                    if continuous_shadow_enabled:
                                        _queue_continuous_shadow(state, {
                                            'analysis_type': 'SIDE_HEALTH_ERROR',
                                            'version': side_scorer_v21.VERSION,
                                            'run_id': getattr(state, 'run_id', None),
                                            'opportunity_id': opportunity_id,
                                            'error_type': type(side_exc).__name__,
                                            'error': str(side_exc)[:300],
                                            'live_authority': False,
                                        }, now_wall)
                        except Exception as exc:
                            state.continuous_shadow_health_errors = int(
                                getattr(state, 'continuous_shadow_health_errors', 0)
                            ) + 1
                            last_error_emit = float(
                                shadow_schedule.get('last_error_emit_mono', 0.0)
                            )
                            if now_mono - last_error_emit >= 5.0:
                                shadow_schedule['last_error_emit_mono'] = now_mono
                                if continuous_shadow_enabled:
                                    _queue_continuous_shadow(state, {
                                        'analysis_type': 'HEALTH_ERROR',
                                        'version': 'CONTINUOUS_SHADOW_V1',
                                        'opportunity_id': opportunity_id,
                                        'setup_id': setup.get('setup_id'),
                                        'error_type': type(exc).__name__,
                                        'error': str(exc)[:300],
                                        'live_authority': False,
                                    }, now_wall)
                            logging.exception(
                                "❌ [CONTINUOUS SHADOW] scorer lỗi; live không bị chặn: %s",
                                exc,
                            )
                    if live_should_score and not passive_pending:
                        setup['last_revision'] = revision
                        setup['last_score_mono'] = now_mono
                        commander_kwargs = {
                            'setup': setup, 'decision_snapshot': decision,
                        }
                        if continuous_live:
                            commander_kwargs['continuous_score'] = continuous_result
                        signal = chi_huy_truong.phan_tich_va_ra_lenh(
                            state, mode_info, candidate['mode'], candidate['bias'],
                            **commander_kwargs,
                        )
                        _sync_breakout_setup_memory(state, setup)
                        if signal is not None and setup.get('state') == 'EXECUTING':
                            setup['claimed_once'] = True

            if setup is not None:
                new_states[key] = setup.get('state', _watch_state())
            else:
                new_states[key] = raw_state
            reference_zone = (
                float(activation_probe['zone'])
                if activation_probe is not None
                else float(setup.get('zone', candidate['zone']))
                if setup is not None
                else float(candidate['zone'])
            )
            diagnostics[key] = {
                'state': new_states[key],
                'reason': activation_reason,
                'price': float(current),
                'zone': reference_zone,
                'live_zone': float(candidate['zone']),
                'atr': atr,
                'distance': float(current) - reference_zone,
                'distance_atr': (
                    (float(current) - reference_zone) / atr if atr > 0.0 else None
                ),
            }

        arm_states = new_states
        state.active_setups = dict(setups)
        if any(value == 'EXECUTING' for value in arm_states.values()):
            state.arm_state = 'EXECUTING'
        elif any(value in ('WATCH', 'ARMED_WINDOW') for value in arm_states.values()):
            state.arm_state = _watch_state()
        elif any(
            value in ('PRE_ARM', 'PRE_ARM_COOLDOWN')
            for value in arm_states.values()
        ):
            state.arm_state = 'PRE_ARM'
        else:
            state.arm_state = 'IDLE'
        state.arm_states = dict(arm_states)
        state.arm_diagnostics = diagnostics
        last_ask, last_bid = current_ask, current_bid
        await asyncio.sleep(RADAR_LOOP_INTERVAL)
