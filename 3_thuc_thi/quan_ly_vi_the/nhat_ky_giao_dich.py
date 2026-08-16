"""Journal theo cycle và mainnet shadow ledger; mọi ghi đĩa chạy nền."""

import asyncio
import importlib.util
import json
import logging
import math
import os
import tempfile
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from loi_he_thong import mainnet_safety


CURRENT_DIR = Path(__file__).resolve().parent
JOURNAL_DIR = Path(os.getenv(
    'SMC_JOURNAL_DIR', str(CURRENT_DIR / 'nhat_ky')
))
SNAPSHOT_PATH = JOURNAL_DIR / 'cycles.json'
EVENT_PATH = JOURNAL_DIR / 'events.jsonl'
MAX_CYCLES_IN_RAM = 300
MAX_COUNTERFACTUALS = 300
MAX_SETUP_OUTCOMES = 300
MAX_SETUP_FOLLOWUPS = 300
MAX_FEE_BLOCKED_SHADOWS = 16
MAX_FEE_BLOCKED_CLUSTERS = 300
MAX_CONTINUOUS_SHADOW_EVENTS = 2000
MAX_CONTINUOUS_SHADOW_DRAIN = 200
MAX_ML_META_EVENTS = 2000
MAX_ML_META_DRAIN = 300
ML_META_DIR = Path(os.getenv(
    'SMC_ML_META_DIR', str(CURRENT_DIR.parents[1] / 'derived' / 'ml_meta' / 'live')
))
ML_META_RETENTION_SECONDS = float(
    os.getenv('SMC_ML_META_RETENTION_SECONDS', str(180 * 86400))
)
ML_META_MAX_BYTES = int(
    os.getenv('SMC_ML_META_MAX_BYTES', str(2 * 1024 * 1024 * 1024))
)
JOURNAL_IDLE_SNAPSHOT_SECONDS = float(
    os.getenv('SMC_JOURNAL_SNAPSHOT_SECONDS', '60')
)
JOURNAL_CRITICAL_SNAPSHOT_SECONDS = float(
    os.getenv('SMC_JOURNAL_CRITICAL_SNAPSHOT_SECONDS', '5')
)
JOURNAL_EVENT_ROTATE_BYTES = int(
    os.getenv('SMC_JOURNAL_EVENT_ROTATE_BYTES', str(128 * 1024 * 1024))
)
JOURNAL_EVENT_RETENTION_SECONDS = float(
    os.getenv('SMC_JOURNAL_EVENT_RETENTION_SECONDS', '86400')
)

# Mainnet only needs enough durable evidence to reconstruct account safety and
# the complete lifecycle of an order.  Producers predating _emit() may append
# high-frequency decision telemetry directly to state.journal_events, so this
# allow-list is enforced again at the disk drain boundary.
MINIMAL_MAINNET_SYSTEM_EVENTS = frozenset({
    'SYSTEM_HEALTH',
    'MAINNET_BREAKER',
    'UNRESOLVED_FORENSIC',
    'STARTUP_FLATTEN',
    'DECISION_AUDIT',
    'BREAKOUT_SETUP_ROLLED',
})


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


economic_mod = _load(
    'journal_economic',
    CURRENT_DIR.parents[1] / '2_suy_luan_mapping' / 'tong_ket_chi_huy' / 'kinh_te_lenh.py',
)
shark_mod = _load(
    'journal_shark',
    CURRENT_DIR.parents[1] / '2_suy_luan_mapping' / 'tong_ket_chi_huy' / 'shark_context.py',
)
risk_mod = _load('journal_risk', CURRENT_DIR / 'tinh_toan_rui_ro.py')


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_allocation(value):
    return max(0.0, min(0.70, _f(value)))


def _update_venue_attribution(state, cycle):
    """Separate strategy truth from Testnet integration artifacts."""
    strategy = cycle.setdefault('strategy_mainnet', {})
    actual_net = cycle.get('actual', {}).get('net_pnl_bps')
    if cycle.get('execution_venue') == 'BINANCE_FUTURES_MAINNET':
        execution = cycle.setdefault('execution_mainnet', {})
        if actual_net is None:
            return
        actual_net = _f(actual_net)
        execution.update({'status': 'CLOSED', 'net_pnl_bps': actual_net})
        strategy.update({
            'status': 'CLOSED', 'net_pnl_bps': actual_net,
            'valid_for_calibration': True,
        })
        cycle['venue_attribution'] = {
            'classification': 'LIVE_MAINNET',
            'exclude_from_calibration': False,
            'strategy_mainnet_net_bps': actual_net,
            'execution_mainnet_net_bps': actual_net,
            'entry_gap_bps': 0.0, 'exit_gap_bps': 0.0,
            'sign_flipped_by_venue': False,
        }
        return
    execution = cycle.setdefault('execution_testnet', {})
    strategy_net = strategy.get('net_pnl_bps')
    if actual_net is not None:
        execution.update({'status': 'CLOSED', 'net_pnl_bps': _f(actual_net)})
    if strategy_net is None or actual_net is None:
        return
    strategy_net = _f(strategy_net)
    actual_net = _f(actual_net)
    entry_gap = abs(_f(cycle.get('actual', {}).get('cross_venue_entry_gap_bps')))
    exit_gap = abs(_f(cycle.get('actual', {}).get('cross_venue_exit_gap_bps')))
    sign_flipped_by_venue = bool(strategy_net > 0.0 and actual_net < 0.0)
    unexplained_gap = abs(actual_net - strategy_net)
    venue_gap_material = entry_gap + exit_gap >= max(1.0, 0.50 * unexplained_gap)
    artifact = bool(sign_flipped_by_venue and venue_gap_material)
    classification = 'VENUE_ARTIFACT' if artifact else 'STRATEGY_OUTCOME'
    previous = (cycle.get('venue_attribution') or {}).get('classification')
    cycle['venue_attribution'] = {
        'classification': classification,
        'exclude_from_calibration': artifact,
        'strategy_mainnet_net_bps': strategy_net,
        'execution_testnet_net_bps': actual_net,
        'entry_gap_bps': entry_gap, 'exit_gap_bps': exit_gap,
        'sign_flipped_by_venue': sign_flipped_by_venue,
    }
    strategy['valid_for_calibration'] = bool(
        strategy.get('valid_for_calibration', True) and not artifact
    )
    if previous != classification:
        _emit(state, 'VENUE_ATTRIBUTION', cycle.get('position_cycle_id'), {
            'classification': classification,
            'strategy_mainnet_net_bps': strategy_net,
            'execution_testnet_net_bps': actual_net,
            'exclude_from_calibration': artifact,
        })


def _ensure(state):
    if not hasattr(state, 'trade_cycles'):
        state.trade_cycles = {}
    if not hasattr(state, 'journal_events'):
        state.journal_events = deque(maxlen=5000)
    if not hasattr(state, 'continuous_shadow_events'):
        state.continuous_shadow_events = deque(
            maxlen=MAX_CONTINUOUS_SHADOW_EVENTS
        )
    if not hasattr(state, 'continuous_shadow_registry'):
        state.continuous_shadow_registry = {}
    if not hasattr(state, 'side_calibration_shadow_registry'):
        state.side_calibration_shadow_registry = {}
    if not hasattr(state, 'unresolved_forensic_fill_ids'):
        state.unresolved_forensic_fill_ids = deque(maxlen=1000)
    if not hasattr(state, 'continuous_shadow_drop_count'):
        state.continuous_shadow_drop_count = 0
    if not hasattr(state, 'continuous_shadow_health_errors'):
        state.continuous_shadow_health_errors = 0
    if not hasattr(state, 'ml_meta_events'):
        state.ml_meta_events = deque(maxlen=MAX_ML_META_EVENTS)
    if not hasattr(state, 'ml_meta_registry'):
        state.ml_meta_registry = {}
    if not hasattr(state, 'ml_meta_drop_count'):
        state.ml_meta_drop_count = 0
    if not hasattr(state, 'ml_meta_health_errors'):
        state.ml_meta_health_errors = 0
    if not hasattr(state, 'shadow_position'):
        state.shadow_position = None
    if not hasattr(state, 'fee_blocked_shadow_positions'):
        state.fee_blocked_shadow_positions = []
    if not hasattr(state, 'fee_blocked_shadow_clusters'):
        state.fee_blocked_shadow_clusters = {}
    if not hasattr(state, 'guardian_counterfactuals'):
        state.guardian_counterfactuals = []
    if not hasattr(state, 'journal_last_trade_time_ms'):
        state.journal_last_trade_time_ms = 0
    if not hasattr(state, 'setup_outcomes'):
        state.setup_outcomes = deque(maxlen=MAX_SETUP_OUTCOMES)
    if not hasattr(state, 'setup_followups'):
        state.setup_followups = deque(maxlen=MAX_SETUP_FOLLOWUPS)


def _emit(state, event_type, cycle_id, payload=None):
    _ensure(state)
    if (
        os.getenv('SMC_MINIMAL_MAINNET_AUDIT', 'false').lower()
        in ('1', 'true', 'yes', 'on')
        and cycle_id is None
        and event_type not in MINIMAL_MAINNET_SYSTEM_EVENTS
    ):
        return
    state.journal_events.append({
        'ts': time.time(), 'event': event_type,
        'position_cycle_id': cycle_id, 'run_id': getattr(state, 'run_id', None),
        'payload': payload or {},
    })


def _keep_minimal_mainnet_event(event):
    """Keep cycle/order audit events and a tiny set of system safety events."""
    if not isinstance(event, dict):
        return False
    if event.get('position_cycle_id') is not None:
        return True
    return event.get('event') in MINIMAL_MAINNET_SYSTEM_EVENTS


def _compact_minimal_mainnet_event(event):
    """Keep actionable decision evidence without persisting hot-path blobs."""
    if not isinstance(event, dict):
        return None
    if _keep_minimal_mainnet_event(event):
        return event
    if event.get('event') != 'DECISION_EVALUATED':
        return None
    payload = event.get('payload') or {}
    score = payload.get('score') or {}
    context = payload.get('context') or {}
    compact = {
        'setup_id': payload.get('setup_id'),
        'opportunity_id': payload.get('opportunity_id'),
        'generation': payload.get('generation'),
        'mode': payload.get('mode'),
        'bias': payload.get('bias'),
        'kind': payload.get('kind'),
        'entry_style': payload.get('entry_style'),
        'result': payload.get('result'),
        'veto_reason': payload.get('veto_reason'),
        'snapshot_revision': payload.get('snapshot_revision'),
        'score_version': score.get('version'),
        'score': score.get('score', score.get('final_score')),
        'confidence': score.get('confidence'),
        'activation': score.get('activation'),
        'trade_power': score.get('trade_power'),
        'activation_floor': score.get('activation_floor'),
        'activated': score.get('activated'),
        'selected_bias': score.get('selected_bias'),
        'target_notional_pct': score.get('target_notional_pct'),
        'impulse_conflict': score.get('impulse_conflict'),
        'best_bid': context.get('best_bid'),
        'best_ask': context.get('best_ask'),
        'atr_1m': context.get('atr_1m'),
        'poc': context.get('poc'),
        'vah': context.get('vah'),
        'val': context.get('val'),
        'trend_m15': context.get('trend_m15'),
        'code_version': payload.get('code_version'),
        'strategy_config_version': payload.get('strategy_config_version'),
    }
    for key in ('size_pct', 'client_order_id'):
        if key in payload:
            compact[key] = payload[key]
    return {
        'ts': event.get('ts', time.time()),
        'event': 'DECISION_AUDIT',
        'run_id': event.get('run_id'),
        'position_cycle_id': None,
        'payload': compact,
    }


def record_decision_stage(state, event_type, payload=None, cycle_id=None):
    """Public non-blocking hook for executor/economic decision stages."""
    _emit(state, event_type, cycle_id, payload)


def record_continuous_shadow(state, payload):
    """Hook riêng; đầy queue thì drop shadow, không đụng journal live."""
    _ensure(state)
    queue = state.continuous_shadow_events
    if queue.maxlen is not None and len(queue) >= queue.maxlen:
        state.continuous_shadow_drop_count += 1
        return False
    queue.append({
        'ts': time.time(), 'event': 'CONTINUOUS_SCORE_SHADOW',
        'run_id': getattr(state, 'run_id', None),
        'position_cycle_id': None, 'payload': payload or {},
    })
    return True


def _prune_cycles(state):
    while len(state.trade_cycles) > MAX_CYCLES_IN_RAM:
        removable = [
            key for key, cycle in state.trade_cycles.items()
            if cycle.get('status') in ('CLOSED', 'ABORTED')
            and cycle.get('shadow', {}).get('status') != 'OPEN'
        ]
        if not removable:
            break
        oldest = min(
            removable,
            key=lambda key: _f(state.trade_cycles[key].get('created_at')),
        )
        del state.trade_cycles[oldest]


def _session_utc(timestamp):
    hour = time.gmtime(timestamp).tm_hour
    if hour < 8:
        return 'ASIA_UTC_00_08'
    if hour < 13:
        return 'LONDON_UTC_08_13'
    if hour < 21:
        return 'NEW_YORK_UTC_13_21'
    return 'LATE_UTC_21_24'


def _volatility_context(state, reference_price):
    atr = _f(getattr(state, 'atr_1m', 0.0))
    atr_bps = atr / reference_price * 10000.0 if reference_price > 0 else None
    ranges = []
    for candle in list(getattr(state, 'klines_m1', []))[-100:]:
        try:
            high = _f(candle[2] if isinstance(candle, (list, tuple)) else candle['h'])
            low = _f(candle[3] if isinstance(candle, (list, tuple)) else candle['l'])
            if high > low > 0:
                ranges.append(high - low)
        except (KeyError, IndexError, TypeError):
            continue
    if not ranges or atr <= 0:
        regime = 'UNCLASSIFIED'
        median_range = None
    else:
        ordered = sorted(ranges)
        median_range = ordered[len(ordered) // 2]
        ratio = atr / median_range if median_range > 0 else 1.0
        regime = 'LOW' if ratio < 0.75 else 'HIGH' if ratio > 1.50 else 'NORMAL'
    return {'atr_1m': atr, 'atr_bps': atr_bps, 'median_m1_range': median_range, 'regime': regime}


def create_cycle(state, signal, quantity, entry_reference_price, economic):
    """Tạo khóa cha trước POST; testnet được gắn nhãn economic invalid rõ ràng."""
    _ensure(state)
    now = time.time()
    setup_id = str(signal.get('setup_id', 'unknown'))
    cycle_id = f"pc_{int(now * 1000)}_{abs(hash((setup_id, signal.get('setup_generation', 0)))) % 1000000:06d}"
    signal_price = _f(signal.get('signal_price'))
    decision_price = _f(signal.get('decision_price'))
    mainnet_execution = getattr(state, 'execution_venue', '') == (
        'BINANCE_FUTURES_MAINNET'
    )
    cycle = {
        'position_cycle_id': cycle_id,
        'run_id': getattr(state, 'run_id', None),
        'code_version': getattr(state, 'code_version', None),
        'strategy_config_version': getattr(
            state, 'strategy_config_version', None
        ),
        'strategy_profile': getattr(state, 'strategy_profile', None),
        'calibration_version': signal.get('calibration_version'),
        'calibration_mode': signal.get('calibration_mode'),
        'calibration_hash': signal.get('calibration_hash'),
        'setup_id': setup_id,
        'setup_generation': int(signal.get('setup_generation', 0)),
        'symbol': 'BTCUSDT',
        'mode': signal.get('mode', ''),
        'setup_type': signal.get('mode', ''),
        'setup_zone': _f(signal.get('setup_zone')) or None,
        'setup_zone_id': signal.get('setup_zone_id'),
        'setup_kind': signal.get('setup_kind'),
        'opportunity_id': signal.get('opportunity_id'),
        'opportunity_event_ids': list(signal.get('opportunity_event_ids', ())),
        'entry_style': signal.get('entry_style'),
        'entry_lifecycle_version': signal.get('entry_lifecycle_version'),
        'retention_initial': {
            'trade_power': signal.get('initial_trade_power'),
            'activation_floor': signal.get('initial_floor'),
            'realizable_edge_lcb': signal.get('initial_edge_lcb'),
        },
        'breakout_target': _f(signal.get('breakout_target')) or None,
        'breakout_target2': _f(signal.get('breakout_target2')) or None,
        'breakout_target_basis': signal.get('breakout_target_basis'),
        'bias': signal.get('bias', ''),
        'created_at': now,
        'session': _session_utc(now),
        'volatility': _volatility_context(state, _f(entry_reference_price)),
        'status': 'ENTRY_SUBMITTING',
        'execution_purpose': 'LIVE_MAINNET' if mainnet_execution else 'INTEGRATION_TEST',
        'economic_result_valid': bool(mainnet_execution),
        'economic_invalid_reason': (
            None if mainnet_execution
            else 'SIGNAL_MAINNET_EXECUTION_TESTNET_VENUE_MISMATCH'
        ),
        'signal_venue': 'BINANCE_FUTURES_MAINNET',
        'execution_venue': (
            'BINANCE_FUTURES_MAINNET' if mainnet_execution
            else 'BINANCE_FUTURES_TESTNET'
        ),
        'signal_price': signal_price,
        'decision_price': decision_price,
        'entry_reference_price': _f(entry_reference_price),
        'decision_to_submit_ms': int(
            max(0.0, now - _f(signal.get('created_at'), now)) * 1000
        ),
        'depth_snapshot_age_ms': int(
            max(0.0, now - _f(getattr(state, 'thoi_gian_so_lenh_cuoi', now), now)) * 1000
        ),
        'requested_qty': _f(quantity),
        'allocation_policy': dict(signal.get('size_policy', {}) or {}),
        'weak_probe_plus_evaluation': dict(
            signal.get('weak_probe_plus_evaluation', {}) or {}
        ),
        'protection_policy': {
            'version': (
                'AUG13_CAUSAL_FULL_EXIT_V1'
                if mainnet_execution and getattr(
                    state, 'strategy_profile', ''
                ) == 'AUG13_EARLY_HYBRID_V1'
                else 'MAINNET_FULL_POSITION_EXIT_V1'
                if mainnet_execution else 'EARLY_PROTECTION_CAP_V1'
            ),
            'max_cumulative_fraction': 1.0 if mainnet_execution else 0.50,
            'protected_reasons': ['SHARK_ADVERSE_CONFIRMED', 'TIME_STOP'],
            'remaining_exit_authority': (
                'FULL_POSITION_ONLY'
                if mainnet_execution else 'SL_TP_OR_NORMAL_STRATEGY_EXIT'
            ),
        },
        'guardian_breakdown': {
            'version': 'AUG13_CAUSAL_GUARDIAN_V1',
            'last': None,
            'history': [],
        } if mainnet_execution and getattr(
            state, 'strategy_profile', ''
        ) == 'AUG13_EARLY_HYBRID_V1' else {},
        'split_sl_policy': dict(signal.get('split_sl_policy', {}) or {}),
        'entry_reason': signal.get('entry_reason', 'CORE_SCORE_PASS'),
        'score_version': signal.get('score_version', 'CORE_V1'),
        'score_100': signal.get('score_100'),
        'continuous_score': dict(signal.get('continuous_score', {}) or {}),
        'legacy_score': dict(signal.get('legacy_score', {}) or {}),
        'score': {
            'total': signal.get('score_total'), 'core': signal.get('score_core'),
            'effective_core': signal.get('score_effective_core'),
            'm15_modifier': signal.get('score_m15_modifier', 0.0),
            'poc_modifier': signal.get('score_poc_modifier', 0.0),
            'shark': signal.get('score_shark'), 'detail': signal.get('score_detail', []),
            'advisory': signal.get('score_advisory', {}),
            'evidence_quality': signal.get('score_evidence_quality', {}),
        },
        'economic_observation': economic,
        'mainnet_risk_plan': dict(
            (economic or {}).get('mainnet_risk_plan', {}) or {}
        ),
        'dynamic_exit_plan': dict(signal.get('exit_plan', {}) or {}),
        'strategy_mainnet': {
            'venue': 'BINANCE_FUTURES_MAINNET',
            'status': 'PENDING', 'net_pnl_bps': None,
            'valid_for_calibration': True,
        },
        ('execution_mainnet' if mainnet_execution else 'execution_testnet'): {
            'venue': (
                'BINANCE_FUTURES_MAINNET' if mainnet_execution
                else 'BINANCE_FUTURES_TESTNET'
            ),
            'status': 'PENDING', 'net_pnl_bps': None,
            'valid_for_strategy_evaluation': bool(mainnet_execution),
        },
        'venue_attribution': {
            'classification': 'PENDING', 'exclude_from_calibration': False,
        },
        'actual': {
            'valid_for_strategy_evaluation': bool(mainnet_execution),
            'orders': [], 'entry_fill_ids': [], 'exit_fill_ids': [],
            'entry_fill_price': None, 'exit_fill_price': None,
            'gross_pnl_quote': None, 'fee_quote': None, 'net_pnl_quote': None,
            'gross_pnl_bps': None, 'fee_bps': None, 'net_pnl_bps': None,
        },
        'shadow': {
            'valid_for_strategy_evaluation': False,
            'venue': 'BINANCE_FUTURES_MAINNET', 'fill_model': 'TOP10_DEPTH_WALK',
            'status': 'PENDING_ACTUAL_ENTRY_CONFIRMATION', 'orders': [],
        },
    }
    state.trade_cycles[cycle_id] = cycle
    _prune_cycles(state)
    _emit(state, 'CYCLE_CREATED', cycle_id, {
        'setup_id': setup_id, 'economic_pass': economic.get('economic_pass'),
        'economic_mode': economic.get('mode'),
    })
    _emit(state, 'ECONOMIC_GATE_EVALUATED', cycle_id, {
        'setup_id': setup_id,
        'result': (
            'PASS' if economic.get('structural_fee_floor_pass') else 'BLOCK'
        ),
        'economic': economic,
    })
    _emit(state, 'WEAK_PROBE_PLUS_EVALUATED', cycle_id, {
        'setup_id': setup_id,
        'evaluation': cycle['weak_probe_plus_evaluation'],
    })
    return cycle_id


def record_actual_order(
    state, cycle_id, role, result, requested_qty, reference_price, reason=None,
    *, strategy_reference_price=None, execution_reference_price=None,
):
    _ensure(state)
    cycle = state.trade_cycles.get(cycle_id)
    if cycle is None:
        return None
    result = result if isinstance(result, dict) else {}
    order = {
        'role': role,
        'reason': reason,
        'order_id': result.get('orderId'),
        'client_order_id': result.get('clientOrderId'),
        'status': result.get('status'),
        'requested_qty': _f(requested_qty),
        'executed_qty_reported': _f(result.get('executedQty')),
        'avg_price_reported': _f(result.get('avgPrice')) or None,
        'reference_price': _f(reference_price),
        'strategy_reference_price_mainnet': _f(
            strategy_reference_price, _f(reference_price)
        ),
        'execution_reference_price_testnet': _f(
            execution_reference_price, _f(reference_price)
        ),
        'submitted_at': time.time(),
        'fills': [],
    }
    cycle['actual']['orders'].append(order)
    if role == 'ENTRY':
        cycle['actual']['entry_order_id'] = order['order_id']
        cycle['actual']['entry_client_order_id'] = order['client_order_id']
    elif role in ('CLOSE', 'TP1', 'PROTECT', 'SL1'):
        cycle['actual']['exit_order_id'] = order['order_id']
        cycle['actual']['exit_client_order_id'] = order['client_order_id']
    _emit(state, 'ACTUAL_ORDER_RECORDED', cycle_id, {
        'role': role, 'order_id': order['order_id'],
        'client_order_id': order['client_order_id'], 'reason': reason,
    })
    return order


def abort_cycle(state, cycle_id, reason):
    cycle = getattr(state, 'trade_cycles', {}).get(cycle_id)
    if cycle is None:
        return
    cycle['status'] = 'ABORTED'
    cycle['abort_reason'] = reason
    cycle['closed_at'] = time.time()
    cycle['shadow']['status'] = 'NOT_OPENED'
    _emit(state, 'CYCLE_ABORTED', cycle_id, {'reason': reason})


def mark_actual_open(state, cycle_id, fill_price, strategy_entry_price=None):
    cycle = getattr(state, 'trade_cycles', {}).get(cycle_id)
    if cycle is None:
        return
    cycle['status'] = 'OPEN'
    cycle['actual']['entry_fill_price'] = _f(fill_price)
    cycle['actual']['strategy_entry_price_mainnet'] = _f(
        strategy_entry_price, _f(cycle.get('entry_reference_price'))
    )
    cycle['actual']['opened_at'] = time.time()
    _emit(state, 'ACTUAL_POSITION_OPEN', cycle_id, {'fill_price': _f(fill_price)})


def mark_actual_closed(
    state, cycle_id, reason, trigger_price, result=None,
    execution_reference_price=None,
):
    cycle = getattr(state, 'trade_cycles', {}).get(cycle_id)
    if cycle is None:
        return
    cycle['status'] = 'CLOSED'
    cycle['exit_reason'] = reason
    cycle['exit_trigger_price'] = _f(trigger_price)
    cycle['exit_decision_price_mainnet'] = _f(trigger_price)
    cycle['exit_execution_reference_price_testnet'] = _f(execution_reference_price)
    cycle['closed_at'] = time.time()
    cycle['holding_time_ms'] = int(
        max(0.0, cycle['closed_at'] - _f(cycle['actual'].get('opened_at'), cycle['created_at']))
        * 1000
    )
    if isinstance(result, dict):
        reported = _f(result.get('avgPrice'))
        if reported > 0:
            cycle['actual']['exit_fill_price'] = reported
    _emit(state, 'ACTUAL_POSITION_CLOSED', cycle_id, {
        'reason': reason,
        'decision_price_mainnet': _f(trigger_price),
        'execution_reference_price_testnet': _f(execution_reference_price),
    })


def _walk_for_side(state, side, quantity, closing=False):
    is_buy = (side == 'LONG') != bool(closing)
    levels = state.asks_top_10 if is_buy else state.bids_top_10
    return economic_mod.estimate_market_fill(levels, quantity)


def _build_shadow_position(state, cycle_id, signal, quantity, entry_estimate, shadow_kind):
    """Tạo position dùng chung cho executed-shadow và fee-blocked shadow."""
    side = signal['bias']
    entry = _f(entry_estimate.get('avg_price'))
    qty = _f(quantity)
    tick = _f(getattr(state, 'exchange_filters', {}).get('tick_size'), 0.1)
    levels = risk_mod.calculate_levels(
        state, entry, side, tick, signal.get('mode', ''),
        setup_zone=signal.get('setup_zone'),
        setup_kind=signal.get('setup_kind'),
        breakout_target=signal.get('breakout_target'),
        breakout_target2=signal.get('breakout_target2'),
        breakout_target_basis=signal.get('breakout_target_basis'),
        exit_plan=signal.get('exit_plan'),
    )
    split_policy = dict(signal.get('split_sl_policy', {}) or {})
    standard_hard_sl = levels['hard_sl']
    if split_policy.get('enabled'):
        standard_hard_sl = _f(
            split_policy.get('standard_hard_sl'), standard_hard_sl
        )
        levels['hard_sl'] = _f(split_policy.get('sl2'), levels['hard_sl'])
    now = _f(entry_estimate.get('captured_at'), time.time())
    entry_fee_bps = (
        economic_mod.PASSIVE_ENTRY_FEE_BPS
        if str(signal.get('entry_style') or '').upper() == 'PASSIVE_RETEST'
        else economic_mod.ENTRY_FEE_BPS
    )
    fee = entry * qty * entry_fee_bps / 10000.0
    return {
        'position_cycle_id': cycle_id, 'shadow_kind': shadow_kind,
        'side': side, 'mode': signal.get('mode', ''),
        'qty': qty, 'initial_qty': qty, 'entry_price': entry,
        'protection_closed_qty': 0.0, 'protection_reasons_done': [],
        'split_sl_enabled': bool(split_policy.get('enabled')),
        'split_sl1_done': False,
        'split_sl1_fraction': _f(split_policy.get('sl1_close_fraction'), 0.90),
        'split_sl1': levels['soft_sl'], 'split_sl2': levels['hard_sl'],
        'standard_hard_sl': standard_hard_sl,
        'opened_at': now, 'hard_sl': levels['hard_sl'], 'soft_sl': levels['soft_sl'],
        'soft_tp1': levels['soft_tp1'], 'soft_tp2': levels['soft_tp2'],
        'tp1_allocation': _f(
            (signal.get('exit_plan') or {}).get('tp1_allocation'), 0.50
        ),
        'tp1_checkpoint_monetizable': bool(
            (signal.get('exit_plan') or {}).get('checkpoint_monetizable', False)
        ),
        'tp1_checkpoint_lock_net_bps': _f(
            (signal.get('exit_plan') or {}).get('checkpoint_lock_net_bps')
        ),
        'runner_policy': (
            (signal.get('exit_plan') or {}).get('runner_policy')
            or 'LEGACY_TP2'
        ),
        'original_hard_sl': levels['hard_sl'], 'original_tp1': levels['soft_tp1'],
        'original_tp2': levels['soft_tp2'], 'expires_at': now + 4500.0,
        'tp1_done': False, 'trailing_active': False, 'tp2_extended': False,
        'add_on_done': False, 'add_on_attempted': False,
        'shark_adverse_since': 0.0, 'shark_support_since': 0.0,
        'gross_pnl_quote': 0.0, 'fee_quote': fee,
        'mfe_bps': 0.0, 'mae_bps': 0.0, 'last_trailing_at': 0.0,
    }, levels, now, fee


def _open_shadow_cycle(state, cycle, position, entry_estimate, levels, now, fee, activation_reason):
    shadow = cycle['shadow']
    shadow.update({
        'valid_for_strategy_evaluation': True, 'status': 'OPEN',
        'shadow_kind': position['shadow_kind'],
        'activation_reason': activation_reason,
        'opened_at': now, 'entry_fill_price': position['entry_price'],
        'entry_reference_price': _f(entry_estimate.get('reference_price')),
        'entry_slippage_bps': _f(entry_estimate.get('slippage_bps')),
        'initial_qty': position['initial_qty'], 'fee_quote': fee,
        'gross_pnl_quote': 0.0, 'levels': dict(levels),
        'MFE_bps': 0.0, 'MAE_bps': 0.0,
    })
    shadow['orders'] = [{
        'role': 'ENTRY', 'ts': now, 'qty': position['initial_qty'],
        'fill_price': position['entry_price'],
        'reference_price': _f(entry_estimate.get('reference_price')),
        'slippage_bps': _f(entry_estimate.get('slippage_bps')),
        'fee_quote': fee, 'fills': entry_estimate.get('fills', []),
    }]


def activate_shadow(state, cycle_id, signal, quantity, entry_estimate):
    """Mở shadow cho lệnh đã execute; không gửi thêm lệnh thật."""
    _ensure(state)
    cycle = getattr(state, 'trade_cycles', {}).get(cycle_id)
    if cycle is None or not entry_estimate.get('available'):
        if cycle is not None:
            cycle['shadow']['status'] = 'UNAVAILABLE'
            cycle['shadow']['invalid_reason'] = entry_estimate.get('reason', 'NO_ENTRY_FILL')
        return False
    if getattr(state, 'shadow_position', None):
        cycle['shadow']['status'] = 'UNAVAILABLE'
        cycle['shadow']['invalid_reason'] = 'ANOTHER_SHADOW_POSITION_ACTIVE'
        return False

    position, levels, now, fee = _build_shadow_position(
        state, cycle_id, signal, quantity, entry_estimate, 'EXECUTED'
    )
    state.shadow_position = position
    _open_shadow_cycle(
        state, cycle, position, entry_estimate, levels, now, fee,
        'ACTUAL_ENTRY_CONFIRMED',
    )
    _emit(state, 'SHADOW_OPEN', cycle_id, {
        'entry': position['entry_price'], 'qty': position['initial_qty'],
        'levels': levels, 'shadow_kind': 'EXECUTED',
    })
    return True


def _semantic_opportunity_key(signal, semantic_key=None):
    key = str(
        signal.get('opportunity_id') or semantic_key
        or signal.get('semantic_key') or signal.get('setup_id', 'unknown')
    )
    head, marker, attempt = key.rpartition(':a')
    return head if marker and attempt.isdigit() else key


def activate_fee_blocked_shadow(
    state, cycle_id, signal, quantity, entry_estimate, semantic_key=None
):
    """Theo dõi full lifecycle của fee-blocked signal, mỗi semantic setup một mẫu."""
    _ensure(state)
    cycle = state.trade_cycles.get(cycle_id)
    if cycle is None or not entry_estimate.get('available'):
        if cycle is not None:
            cycle['shadow']['status'] = 'UNAVAILABLE'
            cycle['shadow']['invalid_reason'] = entry_estimate.get('reason', 'NO_ENTRY_FILL')
        return False

    key = _semantic_opportunity_key(signal, semantic_key)
    existing = state.fee_blocked_shadow_clusters.get(key)
    if existing:
        existing['attempt_count'] = int(existing.get('attempt_count', 1)) + 1
        existing['last_attempt_at'] = time.time()
        duplicate_ids = existing.setdefault('deduped_cycle_ids', [])
        duplicate_ids.append(cycle_id)
        if len(duplicate_ids) > 50:
            del duplicate_ids[:-50]
        cycle['shadow'].update({
            'status': 'DEDUPED_EXISTING_OPPORTUNITY',
            'shadow_kind': 'FEE_BLOCKED',
            'semantic_key': key,
            'valid_for_strategy_evaluation': False,
            'deduped_to_cycle_id': existing['primary_cycle_id'],
            'sample_independence': 'DUPLICATE_RE_SCORE_NOT_A_NEW_TRADE',
        })
        _emit(state, 'FEE_BLOCKED_SHADOW_DEDUPED', cycle_id, {
            'semantic_key': key,
            'primary_cycle_id': existing['primary_cycle_id'],
            'attempt_count': existing['attempt_count'],
        })
        return False

    if len(state.fee_blocked_shadow_positions) >= MAX_FEE_BLOCKED_SHADOWS:
        cycle['shadow'].update({
            'status': 'UNAVAILABLE', 'shadow_kind': 'FEE_BLOCKED',
            'semantic_key': key,
            'valid_for_strategy_evaluation': False,
            'invalid_reason': 'FEE_BLOCKED_SHADOW_CAPACITY_REACHED',
        })
        _emit(state, 'FEE_BLOCKED_SHADOW_CAPACITY_REACHED', cycle_id, {
            'semantic_key': key, 'capacity': MAX_FEE_BLOCKED_SHADOWS,
        })
        return False

    position, levels, now, fee = _build_shadow_position(
        state, cycle_id, signal, quantity, entry_estimate, 'FEE_BLOCKED'
    )
    position['semantic_key'] = key
    state.fee_blocked_shadow_positions.append(position)
    state.fee_blocked_shadow_clusters[key] = {
        'semantic_key': key, 'primary_cycle_id': cycle_id,
        'opened_at': now, 'status': 'OPEN', 'attempt_count': 1,
        'deduped_cycle_ids': [],
    }
    if len(state.fee_blocked_shadow_clusters) > MAX_FEE_BLOCKED_CLUSTERS:
        closed = [
            (cluster.get('closed_at', cluster.get('opened_at', 0.0)), old_key)
            for old_key, cluster in state.fee_blocked_shadow_clusters.items()
            if cluster.get('status') == 'CLOSED' and old_key != key
        ]
        if closed:
            del state.fee_blocked_shadow_clusters[min(closed)[1]]
    _open_shadow_cycle(
        state, cycle, position, entry_estimate, levels, now, fee,
        'STRUCTURAL_FEE_FLOOR_BLOCKED',
    )
    cycle['shadow'].update({
        'semantic_key': key,
        'sample_independence': 'PRIMARY_SEMANTIC_OPPORTUNITY',
        'fee_gate_observation': dict(cycle.get('economic_observation', {})),
    })
    _emit(state, 'FEE_BLOCKED_SHADOW_OPEN', cycle_id, {
        'semantic_key': key, 'entry': position['entry_price'],
        'qty': position['initial_qty'], 'levels': levels,
    })
    return True


def _shadow_pnl(side, entry, exit_price, qty):
    direction = 1.0 if side == 'LONG' else -1.0
    return (exit_price - entry) * qty * direction


def _shadow_close_qty(state, position, quantity, role, reason):
    cycle = state.trade_cycles.get(position['position_cycle_id'])
    fill = _walk_for_side(state, position['side'], quantity, closing=True)
    if cycle is None or not fill.get('available'):
        if cycle is not None:
            cycle['shadow']['data_quality_warning'] = fill.get('reason', 'EXIT_DEPTH_UNAVAILABLE')
        return False
    exit_price = _f(fill.get('avg_price'))
    fee = exit_price * quantity * economic_mod.EXIT_FEE_BPS / 10000.0
    gross = _shadow_pnl(position['side'], position['entry_price'], exit_price, quantity)
    position['gross_pnl_quote'] += gross
    position['fee_quote'] += fee
    position['qty'] = max(0.0, position['qty'] - quantity)
    cycle['shadow']['orders'].append({
        'role': role, 'reason': reason, 'ts': time.time(), 'qty': quantity,
        'fill_price': exit_price, 'reference_price': _f(fill.get('reference_price')),
        'slippage_bps': _f(fill.get('slippage_bps')), 'fee_quote': fee,
        'gross_pnl_quote': gross, 'fills': fill.get('fills', []),
    })
    cycle['shadow']['gross_pnl_quote'] = position['gross_pnl_quote']
    cycle['shadow']['fee_quote'] = position['fee_quote']
    _emit(state, 'SHADOW_ORDER', position['position_cycle_id'], {
        'role': role, 'reason': reason, 'qty': quantity, 'fill_price': exit_price,
    })
    return True


def _shadow_early_protection(state, position, reason, current):
    reasons = position.setdefault('protection_reasons_done', [])
    if reason in reasons:
        return False
    if position.get('split_sl_enabled'):
        reasons.append(reason)
        return False
    step = _f(getattr(state, 'exchange_filters', {}).get('step_size'), 0.001)
    qty = risk_mod.calculate_early_protection_qty(
        position['qty'], position['initial_qty'],
        position.get('protection_closed_qty', 0.0), position['qty'], step,
    )
    min_qty = _f(getattr(state, 'exchange_filters', {}).get('min_qty'), step)
    min_notional = _f(getattr(state, 'exchange_filters', {}).get('min_notional'))
    if qty < max(step, min_qty) or (min_notional and qty * current < min_notional):
        reasons.append(reason)
        return False
    before = position['qty']
    if not _shadow_close_qty(state, position, qty, 'PROTECT', reason):
        return False
    position['protection_closed_qty'] = min(
        position['initial_qty'] * 0.5,
        position.get('protection_closed_qty', 0.0) + max(0.0, before - position['qty']),
    )
    reasons.append(reason)
    return True


def _start_counterfactual(state, position, exit_reason, exit_price):
    if exit_reason not in ('SOFT_SL', 'SHARK_ADVERSE_CONFIRMED'):
        return
    entry_price = position['entry_price']
    guardian_gross_bps = _shadow_pnl(
        position['side'], entry_price, exit_price, 1.0
    ) / entry_price * 10000.0
    cycle = state.trade_cycles.get(position['position_cycle_id'], {})
    observation = cycle.get('economic_observation', {})
    required_gross_bps = _f(
        observation.get('required_capture_bps'),
        economic_mod.ENTRY_FEE_BPS + economic_mod.EXIT_FEE_BPS
        + economic_mod.MINIMUM_NET_EDGE_BPS,
    )
    protect_candidate = exit_reason == 'SHARK_ADVERSE_CONFIRMED' and guardian_gross_bps >= required_gross_bps
    if position['side'] == 'LONG':
        initial_stop = entry_price * (1.0 + required_gross_bps / 10000.0)
    else:
        initial_stop = entry_price * (1.0 - required_gross_bps / 10000.0)
    tracker = {
        'position_cycle_id': position['position_cycle_id'],
        'guardian_exit_reason': exit_reason, 'side': position['side'],
        'entry_price': position['entry_price'], 'guardian_exit_price': exit_price,
        'guardian_exit_gross_bps': guardian_gross_bps,
        'started_at': time.time(), 'hard_sl': position['original_hard_sl'],
        'tp1': position['original_tp1'], 'tp2': position['original_tp2'],
        'expires_at': position['expires_at'], 'checkpoints_bps': {},
        'first_target_after_exit': None, 'normal_exit': None,
        'price_source': 'MAINNET_EXECUTABLE_BBO', 'no_lookahead': True,
        'profit_protect_candidate': {
            'policy': 'TRAIL_INSTEAD_OF_EJECT' if protect_candidate else 'KEEP_GUARDIAN_EXIT',
            'status': 'ACTIVE' if protect_candidate else 'NOT_APPLICABLE',
            'required_gross_bps': required_gross_bps,
            'stop_price': initial_stop if protect_candidate else None,
            'peak_price': exit_price,
            'exit': None,
        },
    }
    state.guardian_counterfactuals.append(tracker)
    if len(state.guardian_counterfactuals) > MAX_COUNTERFACTUALS:
        del state.guardian_counterfactuals[:-MAX_COUNTERFACTUALS]


def _finish_shadow(state, position, reason):
    cycle = state.trade_cycles[position['position_cycle_id']]
    shadow = cycle['shadow']
    shadow['status'] = 'CLOSED'
    shadow['exit_reason'] = reason
    shadow['closed_at'] = time.time()
    shadow['holding_time_ms'] = int((shadow['closed_at'] - position['opened_at']) * 1000)
    shadow['MFE_bps'] = position['mfe_bps']
    shadow['MAE_bps'] = position['mae_bps']
    shadow['gross_pnl_quote'] = position['gross_pnl_quote']
    shadow['fee_quote'] = position['fee_quote']
    shadow['net_pnl_quote'] = position['gross_pnl_quote'] - position['fee_quote']
    initial_notional = position['initial_qty'] * shadow['entry_fill_price']
    if initial_notional > 0:
        shadow['gross_pnl_bps'] = position['gross_pnl_quote'] / initial_notional * 10000.0
        shadow['fee_bps'] = position['fee_quote'] / initial_notional * 10000.0
        shadow['net_pnl_bps'] = shadow['net_pnl_quote'] / initial_notional * 10000.0
    last_order = shadow['orders'][-1] if shadow['orders'] else {}
    shadow['exit_fill_price'] = last_order.get('fill_price')
    cycle.setdefault('strategy_mainnet', {}).update({
        'status': 'CLOSED', 'net_pnl_bps': shadow.get('net_pnl_bps'),
        'exit_reason': reason,
        'valid_for_calibration': not bool(shadow.get('data_quality_warning')),
    })
    plan = dict(cycle.get('dynamic_exit_plan', {}) or {})
    if plan.get('version') == 'DYNAMIC_PATH_V2':
        labels = []
        mfe = _f(shadow.get('MFE_bps'))
        for candidate in plan.get('target_candidates', ()):
            distance = _f(candidate.get('distance_bps'))
            labels.append({
                'target_id': candidate.get('target_id'),
                'hit_before_terminal': bool(distance > 0.0 and mfe >= distance),
                'distance_bps': distance,
                'horizon_minutes': candidate.get('horizon_minutes'),
            })
        cycle['path_calibration_label'] = {
            'label_version': 'PATH_TARGET_LABEL_V1',
            'opportunity_id': cycle.get('opportunity_id') or cycle.get('setup_id'),
            'terminal_reason': reason,
            'mfe_bps': shadow.get('MFE_bps'),
            'mae_bps': shadow.get('MAE_bps'),
            'targets': labels,
            'future_fields_are_labels_only': True,
        }
    _update_venue_attribution(state, cycle)
    _start_counterfactual(state, position, reason, _f(last_order.get('fill_price')))
    _emit(state, 'SHADOW_CLOSED', position['position_cycle_id'], {
        'reason': reason, 'net_pnl_bps': shadow.get('net_pnl_bps'),
        'MFE_bps': shadow.get('MFE_bps'), 'MAE_bps': shadow.get('MAE_bps'),
        'shadow_kind': position.get('shadow_kind', 'EXECUTED'),
    })
    if position.get('shadow_kind') == 'FEE_BLOCKED':
        state.fee_blocked_shadow_positions = [
            item for item in state.fee_blocked_shadow_positions
            if item.get('position_cycle_id') != position['position_cycle_id']
        ]
        key = position.get('semantic_key')
        cluster = state.fee_blocked_shadow_clusters.get(key)
        if cluster is not None:
            cluster['status'] = 'CLOSED'
            cluster['closed_at'] = shadow['closed_at']
            cluster['exit_reason'] = reason
            cluster['net_pnl_bps'] = shadow.get('net_pnl_bps')
    else:
        state.shadow_position = None


def _veto_clear(state, side):
    wall = getattr(state, 'wall_pull_flag', {}) or {}
    if wall.get('active') and (
        (side == 'LONG' and wall.get('side') == 'buy')
        or (side == 'SHORT' and wall.get('side') == 'sell')
    ):
        return False
    threshold = max(_f(getattr(state, 'p95_value', 3.0)) * 3.0, _f(getattr(state, 'vol_pct90', 0.0)))
    if side == 'LONG' and _f(getattr(state, 'current_cvd_sell_3s', 0.0)) > threshold:
        return False
    if side == 'SHORT' and _f(getattr(state, 'current_cvd_buy_3s', 0.0)) > threshold:
        return False
    return True


def _update_counterfactuals(state, now):
    for item in getattr(state, 'guardian_counterfactuals', []):
        if item.get('normal_exit') is not None:
            continue
        side = item['side']
        current = _f(state.best_bid if side == 'LONG' else state.best_ask)
        if current <= 0:
            continue
        held_bps = _shadow_pnl(side, item['entry_price'], current, 1.0) / item['entry_price'] * 10000.0
        candidate = item.get('profit_protect_candidate', {})
        if candidate.get('status') == 'ACTIVE':
            atr = _f(getattr(state, 'atr_1m', 0.0))
            if side == 'LONG':
                candidate['peak_price'] = max(_f(candidate.get('peak_price')), current)
                if atr > 0:
                    candidate['stop_price'] = max(
                        _f(candidate.get('stop_price')), candidate['peak_price'] - 0.5 * atr
                    )
                stop_hit = current <= _f(candidate.get('stop_price'))
                tp2_hit_candidate = current >= item['tp2']
            else:
                peak = _f(candidate.get('peak_price'), current)
                candidate['peak_price'] = min(peak, current)
                if atr > 0:
                    candidate['stop_price'] = min(
                        _f(candidate.get('stop_price')), candidate['peak_price'] + 0.5 * atr
                    )
                stop_hit = current >= _f(candidate.get('stop_price'))
                tp2_hit_candidate = current <= item['tp2']
            candidate_event = (
                'TP2' if tp2_hit_candidate else 'PROFIT_TRAIL' if stop_hit
                else 'HARD_SL' if (
                    current <= item['hard_sl'] if side == 'LONG' else current >= item['hard_sl']
                )
                else 'EXPIRY' if now >= item['expires_at'] else None
            )
            if candidate_event:
                roundtrip_fee_bps = economic_mod.ENTRY_FEE_BPS + economic_mod.EXIT_FEE_BPS
                candidate['status'] = 'CLOSED'
                candidate['exit'] = {
                    'event': candidate_event, 'ts': now, 'price': current,
                    'gross_bps': held_bps,
                    'net_bps': held_bps - roundtrip_fee_bps,
                    'value_vs_guardian_bps': held_bps - item['guardian_exit_gross_bps'],
                }
                _emit(state, 'PROFIT_PROTECT_CANDIDATE_CLOSED', item['position_cycle_id'], candidate['exit'])
        elapsed = now - item['started_at']
        for seconds in (30, 120, 300):
            key = str(seconds)
            if elapsed >= seconds and key not in item['checkpoints_bps']:
                item['checkpoints_bps'][key] = held_bps
                _emit(state, 'COUNTERFACTUAL_CHECKPOINT', item['position_cycle_id'], {
                    'seconds': seconds, 'held_gross_bps': held_bps,
                })
        if item['first_target_after_exit'] is None:
            tp1_hit = current >= item['tp1'] if side == 'LONG' else current <= item['tp1']
            if tp1_hit:
                item['first_target_after_exit'] = {'event': 'TP1', 'ts': now, 'price': current}
        hard_hit = current <= item['hard_sl'] if side == 'LONG' else current >= item['hard_sl']
        tp2_hit = current >= item['tp2'] if side == 'LONG' else current <= item['tp2']
        event = 'HARD_SL' if hard_hit else 'TP2' if tp2_hit else 'EXPIRY' if now >= item['expires_at'] else None
        if event:
            item['normal_exit'] = {'event': event, 'ts': now, 'price': current, 'gross_bps': held_bps}
            item['guardian_value_bps'] = item['guardian_exit_gross_bps'] - held_bps
            _emit(state, 'COUNTERFACTUAL_COMPLETE', item['position_cycle_id'], {
                'normal_exit': event, 'guardian_value_bps': item['guardian_value_bps'],
            })


def _shadow_step_one(state, position, now):
    """Một tick cho đúng một shadow position."""
    cycle = state.trade_cycles.get(position['position_cycle_id'])
    if cycle is None:
        if position.get('shadow_kind') == 'FEE_BLOCKED':
            state.fee_blocked_shadow_positions = [
                item for item in state.fee_blocked_shadow_positions
                if item.get('position_cycle_id') != position['position_cycle_id']
            ]
        elif state.shadow_position is position:
            state.shadow_position = None
        return
    side = position['side']
    current = _f(state.best_bid if side == 'LONG' else state.best_ask)
    if current <= 0:
        return
    move_bps = _shadow_pnl(side, position['entry_price'], current, 1.0) / position['entry_price'] * 10000.0
    position['mfe_bps'] = max(position['mfe_bps'], move_bps)
    position['mae_bps'] = min(position['mae_bps'], move_bps)
    cycle['shadow']['MFE_bps'] = position['mfe_bps']
    cycle['shadow']['MAE_bps'] = position['mae_bps']

    shark = shark_mod.evaluate(state, side, now)
    spread = abs(_f(state.best_ask) - _f(state.best_bid))
    atr = _f(getattr(state, 'atr_1m', 0.0))
    spread_too_high = atr > 0 and spread > 0.5 * atr
    sl_hit = current <= position['soft_sl'] if side == 'LONG' else current >= position['soft_sl']
    sl2_hit = bool(
        position.get('split_sl_enabled')
        and (
            current <= position['hard_sl']
            if side == 'LONG' else current >= position['hard_sl']
        )
    )
    tp1_hit = current >= position['soft_tp1'] if side == 'LONG' else current <= position['soft_tp1']
    tp2_hit = current >= position['soft_tp2'] if side == 'LONG' else current <= position['soft_tp2']

    if sl2_hit and not spread_too_high:
        qty = position['qty']
        if _shadow_close_qty(state, position, qty, 'CLOSE', 'SL2'):
            _finish_shadow(state, position, 'SL2')
        return

    if sl_hit and not spread_too_high:
        if position.get('split_sl_enabled'):
            if not position.get('split_sl1_done'):
                step = _f(
                    getattr(state, 'exchange_filters', {}).get('step_size'), 0.001
                )
                qty = risk_mod.calculate_split_sl1_close_qty(
                    position['qty'], position['initial_qty'], step,
                )
                position['split_sl1_done'] = True
                min_qty = _f(
                    getattr(state, 'exchange_filters', {}).get('min_qty'), step
                )
                min_notional = _f(
                    getattr(state, 'exchange_filters', {}).get('min_notional')
                )
                if (
                    qty >= max(step, min_qty)
                    and (not min_notional or qty * current >= min_notional)
                ):
                    _shadow_close_qty(state, position, qty, 'SL1', 'SL1_90')
            return
        qty = position['qty']
        if _shadow_close_qty(state, position, qty, 'CLOSE', 'SOFT_SL'):
            _finish_shadow(state, position, 'SOFT_SL')
        return

    supportive = (
        shark.get('status') == 'SHARK_SUPPORTIVE'
        and shark.get('support_count', 0) >= 2
        and shark.get('adverse_count', 0) == 0
    )
    if tp2_hit and position['tp1_done'] and supportive:
        position['tp2_extended'] = True
    elif tp2_hit:
        qty = position['qty']
        if _shadow_close_qty(state, position, qty, 'CLOSE', 'TP2'):
            _finish_shadow(state, position, 'TP2')
        return

    if tp1_hit and not position['tp1_done']:
        step = _f(getattr(state, 'exchange_filters', {}).get('step_size'), 0.001)
        allocation = _clamp_allocation(position.get('tp1_allocation', 0.50))
        tp1_size = risk_mod.calculate_tp1_close_qty(
            position['qty'], position['initial_qty'], allocation, current,
            getattr(state, 'exchange_filters', {}),
        )
        qty = tp1_size['quantity']
        if tp1_size['executable'] and _shadow_close_qty(
            state, position, qty, 'TP1', 'TP1'
        ):
            position['tp1_done'] = True
            position['trailing_active'] = True
            position['soft_sl'] = position['entry_price']
        elif not tp1_size['executable']:
            # No realized TP means no trailing activation or break-even stop.
            position['tp1_done'] = True

    status = shark.get('status')
    if status == 'SHARK_ADVERSE':
        position['shark_support_since'] = 0.0
        position['shark_adverse_since'] = position['shark_adverse_since'] or now
        price_damage = (
            _f(getattr(state, 'ema9_m1', 0.0)) > 0
            and ((side == 'LONG' and current < state.ema9_m1) or (side == 'SHORT' and current > state.ema9_m1))
        )
        directional = bool(set(shark.get('adverse', [])).intersection({'FLASH_FLOW', 'FOOTPRINT'}))
        confirmed = now - position['shark_adverse_since'] >= 1.0
        if confirmed and (shark.get('adverse_count', 0) >= 3 or (price_damage and directional)):
            _shadow_early_protection(
                state, position, 'SHARK_ADVERSE_CONFIRMED', current
            )
            return
    elif status == 'SHARK_SUPPORTIVE':
        position['shark_adverse_since'] = 0.0
        position['shark_support_since'] = position['shark_support_since'] or now
    else:
        position['shark_adverse_since'] = 0.0
        position['shark_support_since'] = 0.0

    if now >= position['expires_at']:
        _shadow_early_protection(state, position, 'TIME_STOP', current)
        return

    if (
        not position['add_on_attempted'] and not position['tp1_done']
        and position['mode'].startswith('TREND') and atr > 0
    ):
        profit = current - position['entry_price'] if side == 'LONG' else position['entry_price'] - current
        if profit >= atr and supportive and _veto_clear(state, side):
            position['add_on_attempted'] = True
            step = _f(getattr(state, 'exchange_filters', {}).get('step_size'), 0.001)
            qty = math.floor((position['initial_qty'] * 0.25 + 1e-12) / step) * step
            fill = _walk_for_side(state, side, qty, closing=False)
            if qty >= step and fill.get('available'):
                price = _f(fill.get('avg_price'))
                fee = price * qty * economic_mod.ENTRY_FEE_BPS / 10000.0
                old_qty = position['qty']
                position['entry_price'] = (position['entry_price'] * old_qty + price * qty) / (old_qty + qty)
                position['qty'] += qty
                position['fee_quote'] += fee
                position['add_on_done'] = True
                cycle['shadow']['orders'].append({
                    'role': 'ADD_ON', 'ts': now, 'qty': qty, 'fill_price': price,
                    'fee_quote': fee, 'slippage_bps': _f(fill.get('slippage_bps')),
                    'fills': fill.get('fills', []),
                })
                if side == 'LONG':
                    position['soft_sl'] = max(position['soft_sl'], position['entry_price'])
                else:
                    position['soft_sl'] = min(position['soft_sl'], position['entry_price'])

    if position['trailing_active'] and now - position['last_trailing_at'] >= 0.25:
        position['last_trailing_at'] = now
        ema9 = _f(getattr(state, 'ema9_m1', 0.0))
        tick = _f(getattr(state, 'exchange_filters', {}).get('tick_size'), 0.1)
        if ema9 > 0 and atr > 0:
            buffer_mult = 0.40 if supportive else 0.10 if status == 'SHARK_ADVERSE' else 0.25
            if side == 'LONG':
                candidate = min(ema9 - buffer_mult * atr, _f(state.best_bid) - tick)
                position['soft_sl'] = max(position['soft_sl'], candidate)
            else:
                candidate = max(ema9 + buffer_mult * atr, _f(state.best_ask) + tick)
                position['soft_sl'] = min(position['soft_sl'], candidate)


def shadow_step(state, now=None):
    """Một tick cho executed shadow và mọi fee-blocked shadow độc lập."""
    _ensure(state)
    now = time.time() if now is None else now
    _update_counterfactuals(state, now)
    if state.shadow_position:
        _shadow_step_one(state, state.shadow_position, now)
    # Copy để position có thể tự xóa khỏi danh sách khi đóng mà không skip mẫu kế.
    for position in list(state.fee_blocked_shadow_positions):
        _shadow_step_one(state, position, now)


def _recover_unlinked_close_orders(state, trades, order_index):
    """Never infer a cycle from side/time/qty; preserve unknown fills as evidence."""
    seen = {str(value) for value in state.unresolved_forensic_fill_ids}
    for trade in trades if isinstance(trades, list) else []:
        order_id = str(trade.get('orderId'))
        fill_id = str(trade.get('id'))
        if order_id in order_index or fill_id in seen:
            continue
        state.unresolved_forensic_fill_ids.append(fill_id)
        seen.add(fill_id)
        _emit(state, 'UNRESOLVED_FORENSIC', None, {
            'order_id': trade.get('orderId'), 'fill_id': trade.get('id'),
            'side': trade.get('side'), 'position_side': trade.get('positionSide'),
            'qty': _f(trade.get('qty')), 'price': _f(trade.get('price')),
            'trade_time_ms': int(trade.get('time', 0) or 0),
            'reason': 'NO_EXACT_ORDER_ID_LINK_NO_HEURISTIC_BACKFILL',
        })


def _apply_trade_fills(state, trades):
    order_index = {}
    for cycle in state.trade_cycles.values():
        for order in cycle.get('actual', {}).get('orders', []):
            order_id = order.get('order_id')
            if order_id is not None:
                order_index[str(order_id)] = (cycle, order)
    _recover_unlinked_close_orders(state, trades, order_index)
    for trade in trades if isinstance(trades, list) else []:
        mapped = order_index.get(str(trade.get('orderId')))
        if mapped is None:
            continue
        cycle, order = mapped
        fill_id = str(trade.get('id'))
        if any(str(item.get('fill_id')) == fill_id for item in order['fills']):
            continue
        fill = {
            'fill_id': trade.get('id'), 'order_id': trade.get('orderId'),
            'price': _f(trade.get('price')), 'qty': _f(trade.get('qty')),
            'quote_qty': _f(trade.get('quoteQty')),
            'commission': _f(trade.get('commission')),
            'commission_asset': trade.get('commissionAsset'),
            'realized_pnl': _f(trade.get('realizedPnl')),
            'time': trade.get('time'), 'maker': trade.get('maker'),
            'side': trade.get('side'), 'position_side': trade.get('positionSide'),
        }
        order['fills'].append(fill)
        state.journal_last_trade_time_ms = max(
            int(getattr(state, 'journal_last_trade_time_ms', 0)), int(trade.get('time', 0) or 0)
        )
        target = 'entry_fill_ids' if order['role'] in ('ENTRY', 'ADD_ON') else 'exit_fill_ids'
        cycle['actual'][target].append(trade.get('id'))
        _emit(state, 'ACTUAL_FILL_SYNCED', cycle['position_cycle_id'], {
            'order_id': trade.get('orderId'), 'fill_id': trade.get('id'),
        })

    for cycle in state.trade_cycles.values():
        for order in cycle.get('actual', {}).get('orders', []):
            fill_qty = sum(_f(item.get('qty')) for item in order.get('fills', []))
            fill_fee = sum(_f(item.get('commission')) for item in order.get('fills', []))
            expected = _f(order.get('executed_qty_reported')) or _f(order.get('requested_qty'))
            order['fill_qty'] = fill_qty
            order['fill_fee_quote'] = fill_fee
            order['quantity_reconciled'] = (
                bool(order.get('fills')) and abs(fill_qty - expected) <= max(1e-12, expected * 1e-9)
            )

    for cycle in state.trade_cycles.values():
        fills = [
            (order['role'], fill)
            for order in cycle.get('actual', {}).get('orders', [])
            for fill in order.get('fills', [])
        ]
        if not fills:
            continue
        entry = [fill for role, fill in fills if role in ('ENTRY', 'ADD_ON')]
        exits = [fill for role, fill in fills if role not in ('ENTRY', 'ADD_ON')]
        entry_qty = sum(item['qty'] for item in entry)
        exit_qty = sum(item['qty'] for item in exits)
        if entry_qty > 0:
            cycle['actual']['entry_fill_price'] = sum(item['price'] * item['qty'] for item in entry) / entry_qty
            reference = _f(cycle.get('entry_reference_price'))
            if reference > 0:
                cycle['actual']['cross_venue_entry_gap_bps'] = (
                    cycle['actual']['entry_fill_price'] - reference
                ) / reference * 10000.0
        if exit_qty > 0:
            cycle['actual']['exit_fill_price'] = sum(item['price'] * item['qty'] for item in exits) / exit_qty
            trigger = _f(cycle.get('exit_trigger_price'))
            if trigger > 0:
                cycle['actual']['cross_venue_exit_gap_bps'] = (
                    cycle['actual']['exit_fill_price'] - trigger
                ) / trigger * 10000.0
            execution_reference = _f(
                cycle.get('exit_execution_reference_price_testnet')
            )
            if execution_reference > 0:
                cycle['actual']['execution_exit_slippage_bps'] = (
                    cycle['actual']['exit_fill_price'] - execution_reference
                ) / execution_reference * 10000.0
        fee = sum(item['commission'] for _, item in fills)
        gross = sum(item['realized_pnl'] for item in exits)
        notional = entry_qty * _f(cycle['actual'].get('entry_fill_price'))
        cycle['actual']['gross_pnl_quote'] = gross
        cycle['actual']['fee_quote'] = fee
        cycle['actual']['net_pnl_quote'] = gross - fee
        if entry_qty > 0 and exit_qty >= entry_qty - 1e-9:
            if cycle.get('status') != 'CLOSED':
                cycle['status'] = 'CLOSED'
                cycle['exit_reason'] = cycle.get('exit_reason') or 'FILLS_RECONCILED_CLOSED'
                latest_exit_ms = max(int(item.get('time', 0) or 0) for item in exits)
                cycle['closed_at'] = latest_exit_ms / 1000.0
                opened = _f(cycle.get('actual', {}).get('opened_at'), cycle.get('created_at'))
                cycle['holding_time_ms'] = int(max(0.0, cycle['closed_at'] - opened) * 1000)
        if notional > 0:
            cycle['actual']['gross_pnl_bps'] = gross / notional * 10000.0
            cycle['actual']['fee_bps'] = fee / notional * 10000.0
            cycle['actual']['net_pnl_bps'] = (gross - fee) / notional * 10000.0
        orders = cycle.get('actual', {}).get('orders', [])
        cycle['actual']['integrity'] = {
            'all_orders_quantity_reconciled': bool(orders) and all(
                order.get('quantity_reconciled', False) for order in orders
            ),
            'gross_minus_fee_equals_net': abs(
                (gross - fee) - _f(cycle['actual'].get('net_pnl_quote'))
            ) <= 1e-9,
            'partial_fills_are_not_orders': True,
        }
        _update_venue_attribution(state, cycle)
    mainnet_safety.sync_loss_streak_from_cycles(state)


async def _resolve_missing_order_ids(state, api):
    """Mất POST response vẫn nối lại được order bằng clientOrderId ổn định."""
    for cycle in state.trade_cycles.values():
        for order in cycle.get('actual', {}).get('orders', []):
            if order.get('order_id') is not None or not order.get('client_order_id'):
                continue
            result, status = await api.query_order('BTCUSDT', order['client_order_id'])
            if status == 200 and isinstance(result, dict) and result.get('orderId') is not None:
                order['order_id'] = result['orderId']
                order['status'] = result.get('status', order.get('status'))
                order['executed_qty_reported'] = _f(result.get('executedQty'))
                order['avg_price_reported'] = _f(result.get('avgPrice')) or None
                _emit(state, 'ACTUAL_ORDER_RECONCILED', cycle['position_cycle_id'], {
                    'order_id': result['orderId'],
                    'client_order_id': order['client_order_id'],
                })


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='journal_', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _evaluate_continuous_shadow_economics(data):
    """Background-only economics từ đúng immutable snapshot của Radar."""
    side = str(data.get('side') or '')
    size_pct = _f(data.get('size_pct'))
    best_bid = _f(data.get('best_bid'))
    best_ask = _f(data.get('best_ask'))
    current_price = best_ask if side == 'LONG' else best_bid
    filters = dict(data.get('exchange_filters', {}) or {})
    quantity = risk_mod.calculate_qty(
        _f(data.get('balance_usdt')), size_pct, current_price, filters,
    )
    if side not in ('LONG', 'SHORT') or current_price <= 0.0:
        return {
            'available': False, 'reason': 'INVALID_SIDE_OR_PRICE',
            'quantity': 0.0, 'economic_pass': False,
        }
    if quantity <= 0.0:
        return {
            'available': False, 'reason': 'BELOW_EXCHANGE_MINIMUM',
            'quantity': 0.0, 'economic_pass': False,
            'requested_size_pct': size_pct, 'shadow_size_after_edge_pct': 0.0,
        }
    entry_levels = (
        data.get('asks_top_10', ()) if side == 'LONG'
        else data.get('bids_top_10', ())
    )
    fill = economic_mod.estimate_market_fill(entry_levels, quantity)
    if not fill.get('available'):
        return {
            'available': False, 'reason': fill.get('reason'),
            'quantity': quantity, 'entry_fill_estimate': fill,
            'economic_pass': False, 'shadow_size_after_edge_pct': 0.0,
        }
    entry_price = _f(fill.get('avg_price'), current_price)
    snapshot = SimpleNamespace(
        atr_1m=_f(data.get('atr_1m')),
        poc=_f(data.get('poc')), vah=_f(data.get('vah')),
        val=_f(data.get('val')),
        swing_high_m15=_f(data.get('swing_high_m15')),
        swing_low_m15=_f(data.get('swing_low_m15')),
        sweep_m1=dict(data.get('sweep_m1', {}) or {}),
    )
    tick_size = _f(filters.get('tick_size'), 0.1)
    levels = risk_mod.calculate_levels(
        snapshot, entry_price, side, tick_size,
        mode=str(data.get('mode') or ''),
        setup_zone=_f(data.get('setup_zone')),
        setup_kind=data.get('setup_kind'),
        breakout_target=_f(data.get('breakout_target')),
        breakout_target2=_f(data.get('breakout_target2')),
        breakout_target_basis=data.get('breakout_target_basis'),
        evaluation_time=_f(data.get('snapshot_time'), time.time()),
    )
    geometry_pass, geometry_reason = risk_mod.validate_level_geometry(
        levels, entry_price, side, tick_size, _f(data.get('atr_1m')),
    )
    economic = economic_mod.observe_snapshot(
        side, quantity, levels['soft_tp1'],
        data.get('bids_top_10', ()), data.get('asks_top_10', ()),
        best_bid, best_ask,
        setup_kind=data.get('setup_kind'),
        target_basis=levels.get('target_basis'),
        entry_style=data.get('entry_style'),
    )
    expected_net = _f(economic.get('expected_net_edge_bps'))
    edge_quality = max(0.0, min(1.0, expected_net / 20.0))
    return {
        'available': True,
        'reason': economic.get('reason'),
        'quantity': quantity, 'entry_price': entry_price,
        'levels': levels, 'geometry_pass': bool(geometry_pass),
        'geometry_reason': geometry_reason,
        'economic_pass': bool(economic.get('economic_pass') and geometry_pass),
        'expected_net_edge_bps': economic.get('expected_net_edge_bps'),
        'all_in_cost_bps': economic.get('all_in_cost_bps'),
        'tp1_distance_bps': economic.get('tp1_distance_bps'),
        'entry_fill_estimate': economic.get('entry_fill_estimate'),
        'exit_fill_estimate': economic.get('exit_fill_estimate'),
        'edge_quality': edge_quality,
        'requested_size_pct': size_pct,
        'shadow_size_after_edge_pct': size_pct * edge_quality,
        'snapshot_time': _f(data.get('snapshot_time')),
        'live_authority': False,
    }


def _process_continuous_shadow_event(state, event):
    processed = dict(event)
    payload = dict(processed.get('payload', {}) or {})
    economic_input = payload.pop('economic_input', None)
    if economic_input is not None and payload.get('economic_requested'):
        try:
            economics = _evaluate_continuous_shadow_economics(economic_input)
        except Exception as exc:
            state.continuous_shadow_health_errors += 1
            economics = {
                'available': False, 'reason': 'SHADOW_ECONOMICS_EXCEPTION',
                'error_type': type(exc).__name__, 'error': str(exc)[:300],
                'live_authority': False,
            }
        payload['economics'] = economics
        opportunity_id = payload.get('opportunity_id')
        record = state.continuous_shadow_registry.get(opportunity_id)
        if record is not None:
            record['economics'] = economics
    processed['payload'] = payload
    return processed


def _sync_continuous_shadow_outcomes(state):
    registry = getattr(state, 'continuous_shadow_registry', {}) or {}
    side_registry = getattr(state, 'side_calibration_shadow_registry', {}) or {}
    for outcome in getattr(state, 'setup_outcomes', ()):
        opportunity_id = outcome.get('opportunity_id')
        record = registry.get(opportunity_id)
        followup = outcome.get('followup', {}) or {}
        label = {
            'mfe_bps': _f(followup.get('mfe_bps')),
            'mae_bps': _f(followup.get('mae_bps')),
            'checkpoints': dict(followup.get('checkpoints', {}) or {}),
            'completed': bool(followup.get('completed')),
            'completed_at': followup.get('completed_at'),
        }
        if record is not None and record.get('terminal'):
            record['terminal']['followup'] = dict(label)
        side_record = side_registry.get(opportunity_id)
        if side_record is not None and side_record.get('terminal'):
            side_record['terminal']['followup'] = dict(label)


def _append_events(events):
    if not events:
        return
    EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _rotate_event_log()
    with open(EVENT_PATH, 'a', encoding='utf-8') as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def _append_ml_meta_events(events):
    """Separate append-only research sink; never delays the live event queue."""
    if not events:
        return
    ML_META_DIR.mkdir(parents=True, exist_ok=True)
    by_day = {}
    for event in events:
        ts = _f(event.get('ts'), time.time())
        day = time.strftime('%Y-%m-%d', time.gmtime(ts))
        by_day.setdefault(day, []).append(event)
    for day, rows in by_day.items():
        path = ML_META_DIR / f'{day}.jsonl'
        with open(path, 'a', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(
                    row, ensure_ascii=False, separators=(',', ':')
                ) + '\n')
            handle.flush()
            os.fsync(handle.fileno())


def _prune_ml_meta_files(now=None):
    now = time.time() if now is None else float(now)
    files = sorted(
        ML_META_DIR.glob('*.jsonl'),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
    ) if ML_META_DIR.exists() else []
    cutoff = now - ML_META_RETENTION_SECONDS
    for path in list(files):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                files.remove(path)
        except FileNotFoundError:
            continue
    total = sum(path.stat().st_size for path in files if path.exists())
    for path in files:
        if total <= ML_META_MAX_BYTES:
            break
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except FileNotFoundError:
            continue


def _rotate_event_log(now=None):
    """Bound the hot journal without deleting forensic data newer than 24h."""
    now = time.time() if now is None else float(now)
    try:
        if EVENT_PATH.stat().st_size >= JOURNAL_EVENT_ROTATE_BYTES:
            stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))
            target = EVENT_PATH.with_name(f'events.{stamp}.jsonl')
            suffix = 1
            while target.exists():
                target = EVENT_PATH.with_name(f'events.{stamp}.{suffix}.jsonl')
                suffix += 1
            os.replace(EVENT_PATH, target)
    except FileNotFoundError:
        pass
    cutoff = now - JOURNAL_EVENT_RETENTION_SECONDS
    for archive in EVENT_PATH.parent.glob('events.*.jsonl'):
        try:
            if archive.stat().st_mtime < cutoff:
                archive.unlink()
        except FileNotFoundError:
            continue


def _prune_active_event_log(now=None):
    """Keep the minimal live audit itself within the configured time window."""
    now = time.time() if now is None else float(now)
    cutoff = now - JOURNAL_EVENT_RETENTION_SECONDS
    try:
        with open(EVENT_PATH, 'r', encoding='utf-8') as source:
            retained = []
            for line in source:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if _f(event.get('ts')) >= cutoff:
                    retained.append(line)
    except FileNotFoundError:
        return
    EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='events_prune_', suffix='.tmp', dir=EVENT_PATH.parent
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            target.writelines(retained)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, EVENT_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _compact_shadow_registry(registry, history_tail=3):
    """Persist restart state, not hundreds of duplicate 1 Hz observations."""
    compact = {}
    for key, value in (registry or {}).items():
        if not isinstance(value, dict):
            continue
        row = {name: item for name, item in value.items() if name != 'history'}
        history = value.get('history', ())
        if history_tail and isinstance(history, list):
            row['history'] = history[-history_tail:]
            row['history_count'] = max(
                int(value.get('history_count', 0) or 0), len(history)
            )
        compact[key] = row
    return compact


def _load_snapshot():
    try:
        with open(SNAPSHOT_PATH, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


async def vong_lap_shadow(state):
    logging.info('🪞 [SHADOW] Mainnet ledger + MFE/MAE/counterfactual đã khởi động.')
    while True:
        try:
            shadow_step(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception('❌ [SHADOW] Lỗi quan sát: %s', exc)
        await asyncio.sleep(0.05)


async def vong_lap_nhat_ky(state, api):
    """I/O nền: đồng bộ fill testnet rồi persist snapshot/event."""
    _ensure(state)
    state.journal_loop_heartbeat_mono = time.monotonic()
    saved = await asyncio.to_thread(_load_snapshot)
    if not state.trade_cycles:
        state.trade_cycles = {
            item['position_cycle_id']: item
            for item in saved.get('cycles', [])
            if isinstance(item, dict) and item.get('position_cycle_id')
        }
        state.guardian_counterfactuals = list(saved.get('guardian_counterfactuals', []))
        state.shadow_position = saved.get('shadow_position')
        state.fee_blocked_shadow_positions = list(
            saved.get('fee_blocked_shadow_positions', [])
        )
        state.fee_blocked_shadow_clusters = dict(
            saved.get('fee_blocked_shadow_clusters', {})
        )
        state.journal_last_trade_time_ms = int(saved.get('journal_last_trade_time_ms', 0) or 0)
    if not state.setup_outcomes:
        state.setup_outcomes = deque(
            saved.get('setup_outcomes', []), maxlen=MAX_SETUP_OUTCOMES
        )
    if not state.setup_followups:
        outcome_index = {
            (item.get('setup_id'), int(item.get('generation', 0))): item
            for item in state.setup_outcomes
        }
        active_followups = []
        for saved_item in saved.get('setup_followups', []):
            if (saved_item.get('followup', {}) or {}).get('completed'):
                continue
            key = (
                saved_item.get('setup_id'),
                int(saved_item.get('generation', 0)),
            )
            outcome = outcome_index.get(key)
            if outcome is None:
                outcome = saved_item
                state.setup_outcomes.append(outcome)
                outcome_index[key] = outcome
            else:
                outcome['followup'] = saved_item.get('followup', {})
            active_followups.append(outcome)
        state.setup_followups = deque(
            active_followups, maxlen=MAX_SETUP_FOLLOWUPS
        )
    if not state.continuous_shadow_registry:
        state.continuous_shadow_registry = dict(
            saved.get('continuous_shadow_registry', {}) or {}
        )
    if not state.side_calibration_shadow_registry:
        state.side_calibration_shadow_registry = dict(
            saved.get('side_calibration_shadow_registry', {}) or {}
        )
    if not state.unresolved_forensic_fill_ids:
        state.unresolved_forensic_fill_ids = deque(
            saved.get('unresolved_forensic_fill_ids', ()), maxlen=1000
        )
    state.continuous_shadow_drop_count = int(
        saved.get(
            'continuous_shadow_drop_count',
            getattr(state, 'continuous_shadow_drop_count', 0),
        ) or 0
    )
    state.continuous_shadow_health_errors = int(
        saved.get(
            'continuous_shadow_health_errors',
            getattr(state, 'continuous_shadow_health_errors', 0),
        ) or 0
    )
    minimal = os.getenv('SMC_MINIMAL_MAINNET_AUDIT', 'false').lower() in (
        '1', 'true', 'yes', 'on'
    )
    if minimal:
        # Snapshot restore happens after Radar's startup reset.  Normalize old
        # research counters here as well so health reflects this run, not a
        # queue overflow from a previous shadow-enabled deployment.
        state.continuous_shadow_events.clear()
        state.continuous_shadow_registry.clear()
        state.side_calibration_shadow_registry.clear()
        state.continuous_shadow_drop_count = 0
        state.continuous_shadow_health_errors = 0
    logging.info(
        '📒 [JOURNAL] Cycle→order→fill; mode=%s.',
        'MAINNET_MINIMAL_24H' if minimal else 'FULL_RESEARCH',
    )
    last_fill_sync = 0.0
    last_snapshot = 0.0
    last_ml_prune = 0.0
    last_event_prune = 0.0
    while True:
        try:
            state.journal_loop_heartbeat_mono = time.monotonic()
            now = time.time()
            if now - last_fill_sync >= 15.0 and state.trade_cycles:
                await _resolve_missing_order_ids(state, api)
                start = int(getattr(state, 'journal_last_trade_time_ms', 0) or 0)
                start = max(0, start - 60000) if start else int((now - 86400) * 1000)
                incomplete = [
                    cycle for cycle in state.trade_cycles.values()
                    if cycle.get('status') in ('OPEN', 'ENTRY_UNKNOWN')
                ]
                if incomplete:
                    earliest = int(min(_f(cycle.get('created_at')) for cycle in incomplete) * 1000)
                    start = min(start, max(0, earliest - 60000))
                trades, status = await api.get_account_trades('BTCUSDT', start_time=start)
                if status == 200:
                    _apply_trade_fills(state, trades)
                last_fill_sync = now
            events = []
            while state.journal_events:
                events.append(state.journal_events.popleft())
            if minimal:
                # Defense in depth: several hot-path producers append directly
                # instead of calling _emit().  Never let their decision chatter
                # bypass the Mainnet minimal-audit policy.
                compact_events = []
                for event in events:
                    compact = _compact_minimal_mainnet_event(event)
                    if compact is not None:
                        compact_events.append(compact)
                events = compact_events
                # Shadow persistence is explicitly disabled in Mainnet
                # minimal mode.  Do not leave restored queues permanently full.
                state.continuous_shadow_events.clear()
            if not minimal:
                shadow_events = []
                while (
                    state.continuous_shadow_events
                    and len(shadow_events) < MAX_CONTINUOUS_SHADOW_DRAIN
                ):
                    shadow_events.append(
                        _process_continuous_shadow_event(
                            state, state.continuous_shadow_events.popleft()
                        )
                    )
                _sync_continuous_shadow_outcomes(state)
                events.extend(shadow_events)
            # Event append is the causal source of truth and must not wait for
            # a multi-megabyte snapshot rewrite.
            if events:
                await asyncio.to_thread(_append_events, events)
                state.journal_last_persist_mono = time.monotonic()
            if not minimal:
                ml_events = []
                while state.ml_meta_events and len(ml_events) < MAX_ML_META_DRAIN:
                    ml_events.append(state.ml_meta_events.popleft())
                if ml_events:
                    await asyncio.to_thread(_append_ml_meta_events, ml_events)
                if now - last_ml_prune >= 3600.0:
                    await asyncio.to_thread(_prune_ml_meta_files, now)
                    last_ml_prune = now
            elif now - last_event_prune >= 3600.0:
                await asyncio.to_thread(_prune_active_event_log, now)
                last_event_prune = now
            critical = any(
                cycle.get('status') in ('OPEN', 'ENTRY_SUBMITTING', 'ENTRY_UNKNOWN')
                for cycle in state.trade_cycles.values()
            )
            snapshot_interval = (
                JOURNAL_CRITICAL_SNAPSHOT_SECONDS
                if critical else JOURNAL_IDLE_SNAPSHOT_SECONDS
            )
            if now - last_snapshot < snapshot_interval:
                await asyncio.sleep(2.0)
                continue
            if minimal:
                cutoff = now - JOURNAL_EVENT_RETENTION_SECONDS
                state.trade_cycles = {
                    key: cycle for key, cycle in state.trade_cycles.items()
                    if cycle.get('status') not in ('CLOSED', 'ABORTED')
                    or float(cycle.get('closed_at', 0.0) or 0.0) >= cutoff
                }
            snapshot = {
                'schema_version': 2, 'updated_at': now,
                'run_id': getattr(state, 'run_id', None),
                'code_version': getattr(state, 'code_version', None),
                'strategy_config_version': getattr(
                    state, 'strategy_config_version', None
                ),
                'cycles': list(state.trade_cycles.values()),
                'guardian_counterfactuals': (
                    [] if minimal else list(state.guardian_counterfactuals)
                ),
                'shadow_position': None if minimal else state.shadow_position,
                'fee_blocked_shadow_positions': (
                    [] if minimal else list(state.fee_blocked_shadow_positions)
                ),
                'fee_blocked_shadow_clusters': (
                    {} if minimal else dict(state.fee_blocked_shadow_clusters)
                ),
                'journal_last_trade_time_ms': int(state.journal_last_trade_time_ms),
                'setup_outcomes': [] if minimal else list(state.setup_outcomes),
                'setup_followups': [] if minimal else list(state.setup_followups),
                'continuous_shadow_registry': (
                    {} if minimal else _compact_shadow_registry(
                        state.continuous_shadow_registry
                    )
                ),
                'side_calibration_shadow_registry': (
                    {} if minimal else _compact_shadow_registry(
                        state.side_calibration_shadow_registry
                    )
                ),
                'unresolved_forensic_fill_ids': list(
                    state.unresolved_forensic_fill_ids
                ),
                'continuous_shadow_drop_count': int(
                    state.continuous_shadow_drop_count
                ),
                'continuous_shadow_health_errors': int(
                    state.continuous_shadow_health_errors
                ),
                'ml_meta_mode': getattr(state, 'ml_meta_mode', 'SHADOW'),
                'ml_meta_artifact_status': getattr(
                    state, 'ml_meta_artifact_status', None
                ),
                'ml_meta_drop_count': int(state.ml_meta_drop_count),
                'ml_meta_health_errors': int(state.ml_meta_health_errors),
            }
            await asyncio.to_thread(_atomic_json, SNAPSHOT_PATH, snapshot)
            state.journal_last_persist_mono = time.monotonic()
            last_snapshot = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception('❌ [JOURNAL] Lỗi đồng bộ: %s', exc)
        await asyncio.sleep(2.0)
