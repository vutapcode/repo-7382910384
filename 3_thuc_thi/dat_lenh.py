"""Executor: tiêu thụ signal, mở vị thế và bắt buộc xác nhận Hard SL."""

import asyncio
import importlib.util
import logging
import math
import os
import time
from decimal import (
    Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP,
)
from pathlib import Path

from loi_he_thong import mainnet_safety, strategy_profile

try:
    from loi_he_thong.order_identity import client_order_id as forensic_order_id
except ModuleNotFoundError:
    _identity_spec = importlib.util.spec_from_file_location(
        'executor_order_identity',
        Path(__file__).resolve().parents[1] / 'loi_he_thong' / 'order_identity.py',
    )
    _identity_mod = importlib.util.module_from_spec(_identity_spec)
    _identity_spec.loader.exec_module(_identity_mod)
    forensic_order_id = _identity_mod.client_order_id


CURRENT_DIR = Path(__file__).resolve().parent


def load_module(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


risk = load_module("tinh_toan_rui_ro", CURRENT_DIR / "quan_ly_vi_the" / "tinh_toan_rui_ro.py")
guardian = load_module("bao_ve_khan_cap", CURRENT_DIR / "ve_si_lenh" / "bao_ve_khan_cap.py")
snapshot_mod = load_module(
    "decision_snapshot",
    CURRENT_DIR.parent / "2_suy_luan_mapping" / "tong_ket_chi_huy" / "decision_snapshot.py",
)
veto_mod = load_module(
    "kiem_duyet_veto",
    CURRENT_DIR.parent / "2_suy_luan_mapping" / "tong_ket_chi_huy" / "kiem_duyet_veto.py",
)
economic_mod = load_module(
    "kinh_te_lenh",
    CURRENT_DIR.parent / "2_suy_luan_mapping" / "tong_ket_chi_huy" / "kinh_te_lenh.py",
)
dynamic_path_mod = load_module(
    "dynamic_path_fee",
    CURRENT_DIR.parent / "2_suy_luan_mapping" / "tong_ket_chi_huy" / "dynamic_path_fee.py",
)
journal_mod = load_module(
    "nhat_ky_giao_dich_executor",
    CURRENT_DIR / "quan_ly_vi_the" / "nhat_ky_giao_dich.py",
)


PREFLIGHT_DECISION_LEASE_SECONDS = 0.50
PREFLIGHT_MAX_ENTRY_DRIFT_ATR = 0.25
PREFLIGHT_ZONE_FAILURE_ATR = 1.00
PREFLIGHT_BREAKOUT_FAILURE_ATR = 0.25
WEAK_PROBE_MIN_EXPECTED_NET_EDGE_BPS = 6.0
PASSIVE_INTENT_BASE_SECONDS = 30.0
PASSIVE_INTENT_CONFIDENCE_SECONDS = 60.0
PASSIVE_REPRICE_MIN_SECONDS = 1.0
PASSIVE_REPRICE_TICKS = 2.0
PASSIVE_REPRICE_MAX = int(os.getenv('SMC_PASSIVE_REPRICE_MAX', '8'))
PASSIVE_RETRY_MARK_SECONDS = 2.0
PASSIVE_TOXICITY_HOLD_MIN = 0.60
ENTRY_GATE_RETRY_SECONDS = 1.0
RETENTION_MIN_POLICY_QTY_BTC = 0.001
RETENTION_MAX_NOTIONAL_PCT = 2.0


def _canonical_stop_price(value, tick_size, position_side):
    """Return a Binance-safe tick-aligned decimal string.

    Float residues such as ``62850.700000000004`` are rejected by the algo
    endpoint.  Quantize toward the entry (LONG=ceil, SHORT=floor), so the
    serialization step can never silently widen the pre-approved loss budget.
    """
    try:
        price = Decimal(str(value))
        tick = Decimal(str(tick_size))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('invalid hard-SL price/tick') from exc
    if not price.is_finite() or not tick.is_finite() or price <= 0 or tick <= 0:
        raise ValueError('invalid hard-SL price/tick')
    quotient = price / tick
    nearest = quotient.to_integral_value(rounding=ROUND_HALF_UP)
    # Values produced by binary-float arithmetic may sit a few trillionths of
    # a tick away from an otherwise exact filter price.  Snap only that tiny
    # residue; real off-tick prices are still rounded toward the entry.
    if abs(quotient - nearest) <= Decimal('1e-8'):
        ticks = nearest
    else:
        rounding = ROUND_CEILING if position_side == 'LONG' else ROUND_FLOOR
        ticks = quotient.to_integral_value(rounding=rounding)
    aligned = ticks * tick
    decimals = max(0, -tick.normalize().as_tuple().exponent)
    return format(aligned, f'.{decimals}f')


def _retryable_algo_failure(status, result):
    """Retry transport/server ambiguity, never deterministic client errors."""
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 599
    code = result.get('code') if isinstance(result, dict) else None
    if status in (408, 409, 425, 429, 599) or status >= 500:
        return True
    # Binance validation/filter/auth errors are deterministic for this payload.
    if 400 <= status < 500 or (isinstance(code, int) and code < 0):
        return False
    return status <= 0


def _dynamic_path_enabled():
    return str(os.getenv('SMC_ECONOMIC_ENGINE', 'STATIC_V1')).upper() == (
        'DYNAMIC_PATH_V2'
    )


def _opportunity_retention_enabled():
    return str(
        os.getenv('SMC_ENTRY_LIFECYCLE', 'LEGACY')
    ).strip().upper() == 'OPPORTUNITY_RETENTION_V1'


def _retention_policy(signal):
    """Freeze a bounded exit hysteresis from the score that earned the claim."""
    score = signal.get('continuous_score') or {}
    initial_power = float(
        signal.get('initial_trade_power', score.get('trade_power', 0.0)) or 0.0
    )
    initial_floor = max(1e-9, float(
        signal.get('initial_floor', score.get('activation_floor', 100.0)) or 100.0
    ))
    margin_ratio = max(0.0, min(
        1.0, (initial_power - initial_floor) / initial_floor
    ))
    return {
        'version': 'OPPORTUNITY_RETENTION_V1',
        'initial_trade_power': initial_power,
        'initial_floor': initial_floor,
        'initial_edge_lcb': signal.get('initial_edge_lcb'),
        'margin_ratio': margin_ratio,
        'retention_floor': initial_floor * (0.75 - 0.25 * margin_ratio),
        'below_floor_grace_seconds': 8.0 + 37.0 * margin_ratio,
    }


def _retention_size_feasibility(equity, desired_pct, price, filters):
    """Apply the explicit 0.001 BTC miss-catcher minimum within a 2% cap."""
    desired = max(0.0, min(float(desired_pct or 0.0), RETENTION_MAX_NOTIONAL_PCT))
    policy_filters = dict(filters or {})
    policy_filters['min_qty'] = max(
        float(policy_filters.get('min_qty', 0.0) or 0.0),
        RETENTION_MIN_POLICY_QTY_BTC,
    )
    probe = risk.quantity_feasibility(equity, desired, price, policy_filters)
    if probe.get('executable') or desired <= 0.0:
        probe['policy_minimum_applied'] = False
        return probe
    minimum_pct = float(probe.get('minimum_executable_notional_pct', 0.0) or 0.0)
    if 0.0 < minimum_pct <= RETENTION_MAX_NOTIONAL_PCT:
        probe = risk.quantity_feasibility(
            equity, minimum_pct + 1e-9, price, policy_filters
        )
        probe.update({
            'policy_minimum_applied': bool(probe.get('executable')),
            'requested_target_notional_pct': desired,
            'effective_target_notional_pct': minimum_pct,
            'policy_minimum_qty_btc': RETENTION_MIN_POLICY_QTY_BTC,
            'policy_cap_notional_pct': RETENTION_MAX_NOTIONAL_PCT,
        })
    return probe


def _dynamic_bundle(
    state, signal, quantity, current_price, tick_size, *, allow_split_sl=True
):
    """Freeze one mainnet snapshot and price a complete two-leg exit plan."""
    bias = signal['bias']
    entry_levels = state.asks_top_10 if bias == 'LONG' else state.bids_top_10
    exit_levels = state.bids_top_10 if bias == 'LONG' else state.asks_top_10
    entry_fill = economic_mod.estimate_market_fill(entry_levels, quantity)
    entry_fill['captured_at'] = time.time()
    exit_fill = economic_mod.estimate_market_fill(exit_levels, quantity)
    entry_price = float(entry_fill.get('avg_price', 0.0) or current_price)
    base_levels = risk.calculate_levels(
        state, entry_price, bias, tick_size, signal.get('mode', ''),
        setup_zone=signal.get('setup_zone'),
        setup_kind=signal.get('setup_kind'),
        breakout_target=signal.get('breakout_target'),
        breakout_target2=signal.get('breakout_target2'),
        breakout_target_basis=signal.get('breakout_target_basis'),
    )
    split_policy = (
        risk.build_value_area_split_sl_policy(
            bias, entry_price, signal.get('decision_poc'),
            signal.get('decision_vah'), signal.get('decision_val'),
            signal.get('score_poc_modifier'),
            float(getattr(state, 'atr_1m', 0.0) or 0.0), tick_size,
            base_levels,
        )
        if allow_split_sl
        else {'enabled': False, 'version': 'MAINNET_FULL_EXIT_V1'}
    )
    effective_stop = float(base_levels['soft_sl'])
    if split_policy.get('enabled'):
        fraction = float(split_policy.get('sl1_close_fraction', 0.90) or 0.90)
        effective_stop = (
            fraction * float(base_levels['soft_sl'])
            + (1.0 - fraction) * float(split_policy['sl2'])
        )
    setup = _find_setup(
        state, signal.get('setup_id'), signal.get('setup_generation', 0)
    )
    snapshot = snapshot_mod.capture(state, setup)
    passive_entry = str(signal.get('entry_style') or '').upper() == 'PASSIVE_RETEST'
    entry_fee_bps = (
        economic_mod.PASSIVE_ENTRY_FEE_BPS
        if passive_entry else economic_mod.ENTRY_FEE_BPS
    )
    plan = dynamic_path_mod.plan_exit(
        snapshot, signal, quantity, entry_price, effective_stop, tick_size,
        filters=state.exchange_filters,
        entry_fee_bps=entry_fee_bps,
        exit_fee_bps=economic_mod.EXIT_FEE_BPS,
        entry_slippage_bps=float(entry_fill.get('slippage_bps', 0.0) or 0.0),
        exit_slippage_bps=float(exit_fill.get('slippage_bps', 0.0) or 0.0),
    )
    levels = risk.calculate_levels(
        state, entry_price, bias, tick_size, signal.get('mode', ''),
        setup_zone=signal.get('setup_zone'),
        setup_kind=signal.get('setup_kind'),
        breakout_target=signal.get('breakout_target'),
        breakout_target2=signal.get('breakout_target2'),
        breakout_target_basis=signal.get('breakout_target_basis'),
        exit_plan=plan if plan.get('available') else None,
    )
    split_policy = (
        risk.build_value_area_split_sl_policy(
            bias, entry_price, signal.get('decision_poc'),
            signal.get('decision_vah'), signal.get('decision_val'),
            signal.get('score_poc_modifier'),
            float(getattr(state, 'atr_1m', 0.0) or 0.0), tick_size, levels,
        )
        if allow_split_sl
        else {'enabled': False, 'version': 'MAINNET_FULL_EXIT_V1'}
    )
    if split_policy.get('enabled'):
        levels['standard_hard_sl'] = levels['hard_sl']
        levels['hard_sl'] = split_policy['sl2']
    risk_plan = None
    if mainnet_safety.execution_venue() == 'MAINNET':
        bid = float(getattr(state, 'execution_best_bid', 0.0) or 0.0)
        ask = float(getattr(state, 'execution_best_ask', 0.0) or 0.0)
        mid = (bid + ask) / 2.0 if min(bid, ask) > 0.0 else entry_price
        spread_bps = (
            max(0.0, ask - bid) / mid * 10000.0 if mid > 0.0 else 0.0
        )
        levels, risk_plan = mainnet_safety.apply_mainnet_loss_budget(
            levels, bias, entry_price, quantity,
            float(getattr(state, 'atr_1m', 0.0) or 0.0), tick_size,
            spread_bps, entry_fee_bps, economic_mod.EXIT_FEE_BPS,
            0.0 if passive_entry else float(
                entry_fill.get('slippage_bps', 0.0) or 0.0
            ),
            float(exit_fill.get('slippage_bps', 0.0) or 0.0),
        )
        levels['mainnet_risk_plan'] = dict(risk_plan)
    economic = economic_mod.observe(
        state, bias, quantity, levels['soft_tp1'],
        setup_kind=signal.get('setup_kind'),
        target_basis=levels.get('target_basis'),
        entry_style=signal.get('entry_style'), exit_plan=plan,
    )
    if risk_plan is not None:
        economic['mainnet_risk_plan'] = dict(risk_plan)
    return entry_fill, entry_price, levels, split_policy, plan, economic


def order_mode_kwargs(state, bias, closing=False):
    if getattr(state, 'account_hedge_mode', True):
        return {'positionSide': bias}
    return {'reduceOnly': 'true'} if closing else {}


def _find_algo_by_client_id(open_algos, client_algo_id):
    if not isinstance(open_algos, list):
        return None
    return next(
        (order for order in open_algos if order.get('clientAlgoId') == client_algo_id),
        None,
    )


async def wait_for_exchange_position(state, api, bias, symbol='BTCUSDT'):
    """Đợi Binance nhìn thấy fill trước khi gửi conditional SL."""
    wanted_side = bias if state.account_hedge_mode else 'BOTH'
    for attempt in range(8):
        positions, status = await api.get_positions(symbol)
        if status == 200:
            found = next(
                (
                    item for item in positions
                    if item.get('positionSide') == wanted_side
                    and abs(float(item.get('positionAmt', 0.0))) > 0
                ),
                None,
            )
            if found is not None:
                return found
        if attempt < 7:
            await asyncio.sleep(0.15)
    return None


def _find_setup(state, setup_id, generation):
    for setup in getattr(state, 'active_setups', {}).values():
        if (
            setup.get('setup_id') == setup_id
            and int(setup.get('generation', 0)) == int(generation)
        ):
            return setup
    return None


def release_execution(state, signal, setup_state='ARMED_WINDOW'):
    """Nhả claim khi chắc chắn không có lệnh mơ hồ trên sàn."""
    setup = _find_setup(
        state, signal.get('setup_id'), signal.get('setup_generation', 0)
    )
    if setup is not None and setup.get('state') == 'EXECUTING':
        setup['state'] = setup_state
    state.execution_in_flight = False
    state.execution_setup_id = None
    state.execution_generation = 0
    state.execution_client_order_id = None
    state.execution_unknown = False
    state.execution_unknown_since = 0.0
    state.last_execution_release_mono = time.monotonic()


def _mark_intent_terminal(state, signal, reason):
    setup = _find_setup(
        state, signal.get('setup_id'), signal.get('setup_generation', 0)
    )
    opportunity_id = signal.get('opportunity_id') or (
        setup.get('semantic_key') if setup else None
    )
    if not opportunity_id:
        return
    registry = getattr(state, 'intent_terminal_opportunities', None)
    if not isinstance(registry, dict):
        registry = {}
        state.intent_terminal_opportunities = registry
    registry[str(opportunity_id)] = {
        'reason': str(reason), 'terminal_at': time.time(),
        'structure_version': int(
            setup.get('structure_version', -1) if setup else -1
        ),
        'setup_id': signal.get('setup_id'),
        'position_cycle_id': signal.get('position_cycle_id'),
    }
    while len(registry) > 512:
        registry.pop(next(iter(registry)))


def _opportunity_key(signal, setup=None):
    return str(
        signal.get('opportunity_id')
        or (setup or {}).get('opportunity_id')
        or (setup or {}).get('semantic_key')
        or ''
    )


def _retry_cycle_registry(state):
    registry = getattr(state, 'opportunity_retry_cycles', None)
    if not isinstance(registry, dict):
        registry = {}
        state.opportunity_retry_cycles = registry
    return registry


def _clear_retry_cycle(state, signal, setup=None):
    opportunity_id = _opportunity_key(signal, setup)
    if opportunity_id:
        _retry_cycle_registry(state).pop(opportunity_id, None)


def _mark_cycle_retry_wait(state, signal, setup, cycle_id, reason):
    cycle = getattr(state, 'trade_cycles', {}).get(cycle_id)
    if cycle is None:
        return False
    opportunity_id = _opportunity_key(signal, setup)
    if not opportunity_id:
        return False
    now = time.time()
    cycle['status'] = 'RETRY_WAIT'
    cycle['retry_wait_count'] = int(cycle.get('retry_wait_count', 0)) + 1
    cycle['last_retry_wait'] = {
        'reason': str(reason), 'ts': now,
        'setup_id': signal.get('setup_id'),
        'setup_generation': int(signal.get('setup_generation', 0) or 0),
    }
    cycle.pop('closed_at', None)
    registry = _retry_cycle_registry(state)
    registry[opportunity_id] = cycle_id
    while len(registry) > 512:
        registry.pop(next(iter(registry)))
    if setup is not None:
        setup['position_cycle_id'] = cycle_id
        setup['passive_intent_active'] = False
        setup['passive_intent_state'] = 'RETRY_WAIT'
        setup['expires_mono'] = max(
            float(setup.get('expires_mono', 0.0) or 0.0),
            time.monotonic() + 60.0,
        )
        cooldowns = getattr(state, 'setup_cooldowns', None)
        if isinstance(cooldowns, dict):
            retry_after = time.monotonic() + ENTRY_GATE_RETRY_SECONDS
            for key in (
                setup.get('setup_id'), setup.get('semantic_key'),
                setup.get('opportunity_id'),
            ):
                if key:
                    cooldowns[str(key)] = max(
                        float(cooldowns.get(str(key), 0.0) or 0.0),
                        retry_after,
                    )
    journal_mod.record_decision_stage(state, 'OPPORTUNITY_RETRY_WAIT', {
        'opportunity_id': opportunity_id,
        'setup_id': signal.get('setup_id'),
        'reason': str(reason),
        'retry_wait_count': cycle['retry_wait_count'],
    }, cycle_id=cycle_id)
    return True


def _create_or_reuse_cycle(state, signal, setup, quantity, price, economic):
    opportunity_id = _opportunity_key(signal, setup)
    cycle_id = _retry_cycle_registry(state).get(opportunity_id)
    cycle = getattr(state, 'trade_cycles', {}).get(cycle_id)
    if cycle_id and cycle and cycle.get('status') == 'RETRY_WAIT':
        now = time.time()
        cycle['status'] = 'ENTRY_SUBMITTING'
        cycle['latest_setup_id'] = signal.get('setup_id')
        cycle['latest_setup_generation'] = int(
            signal.get('setup_generation', 0) or 0
        )
        cycle['entry_reference_price'] = float(price)
        cycle['decision_price'] = float(signal.get('decision_price', price) or price)
        cycle['economic_observation'] = economic
        setup_ids = list(cycle.get('setup_ids', ()))
        if signal.get('setup_id') not in setup_ids:
            setup_ids.append(signal.get('setup_id'))
        cycle['setup_ids'] = setup_ids[-32:]
        journal_mod.record_decision_stage(state, 'OPPORTUNITY_CYCLE_REUSED', {
            'opportunity_id': opportunity_id,
            'setup_id': signal.get('setup_id'),
            'retry_wait_count': int(cycle.get('retry_wait_count', 0)),
            'reused_at': now,
        }, cycle_id=cycle_id)
        return cycle_id
    cycle_id = journal_mod.create_cycle(
        state, signal, quantity, price, economic
    )
    cycle = getattr(state, 'trade_cycles', {}).get(cycle_id)
    if cycle is not None:
        cycle['setup_ids'] = [signal.get('setup_id')]
    return cycle_id


def complete_execution(state, signal):
    setup = _find_setup(
        state, signal.get('setup_id'), signal.get('setup_generation', 0)
    )
    if setup is not None:
        setup['state'] = 'EXECUTED'
        opportunity_id = setup.get('opportunity_id')
        if opportunity_id:
            for opportunity in getattr(state, 'breakout_opportunities', {}).values():
                if opportunity.get('opportunity_id') == opportunity_id:
                    opportunity['state'] = 'EXECUTED'
                    break
    state.setup_cooldowns[signal['setup_id']] = time.monotonic() + 60.0
    if setup is not None and setup.get('semantic_key'):
        state.setup_cooldowns[setup['semantic_key']] = time.monotonic() + 60.0
        state.rearm_blocks[setup['semantic_key']] = {
            'zone': float(setup.get('zone', 0.0) or 0.0),
            'blocked_at_mono': time.monotonic(),
        }
    for event_id in signal.get('event_ids', []):
        state.consumed_market_events[event_id] = signal['setup_id']
    while len(state.consumed_market_events) > 512:
        state.consumed_market_events.pop(next(iter(state.consumed_market_events)))
    state.execution_in_flight = False
    state.execution_setup_id = None
    state.execution_generation = 0
    state.execution_client_order_id = None
    state.execution_unknown = False
    state.execution_unknown_since = 0.0
    state.last_execution_release_mono = time.monotonic()


def _setup_identity_matches(signal, setup, tick_size):
    """Verify that Executor is acting on the exact frozen Radar opportunity."""
    for signal_field, setup_field in (
        ('setup_zone_id', 'zone_id'),
        ('setup_kind', 'kind'),
        ('opportunity_id', 'opportunity_id'),
    ):
        claimed = signal.get(signal_field)
        live = setup.get(setup_field)
        if claimed is not None and live is not None and claimed != live:
            return False, f"Claim không khớp {signal_field}"

    claimed_zone = float(signal.get('setup_zone', 0.0) or 0.0)
    live_zone = float(setup.get('zone', 0.0) or 0.0)
    if claimed_zone > 0.0 and live_zone > 0.0:
        tolerance = max(float(tick_size or 0.0), 1e-9)
        if abs(claimed_zone - live_zone) > tolerance:
            return False, 'Claim không khớp setup_zone'
    return True, 'MATCH'


def _price_drift_guard(signal, snapshot, tick_size):
    """Bound chase/slippage and reject a fresh structural zone failure.

    CORE belongs to the immutable Commander snapshot. This guard intentionally
    checks only price movement that can invalidate execution quality or the
    frozen setup location during the short decision lease.
    """
    bias = signal.get('bias')
    current = float(
        snapshot.best_ask if bias == 'LONG' else snapshot.best_bid
    )
    decision = float(
        signal.get('decision_price', signal.get('signal_price', 0.0)) or 0.0
    )
    atr = float(getattr(snapshot, 'atr_1m', 0.0) or 0.0)
    tick = max(float(tick_size or 0.0), 1e-9)
    if current <= 0.0 or decision <= 0.0 or atr <= 0.0:
        return False, 'Thiếu giá/ATR của decision lease'

    # Implementation shortfall: buying materially higher or selling materially
    # lower than the atomic decision is a worse entry even if CORE still exists.
    adverse_entry_drift = (
        current - decision if bias == 'LONG' else decision - current
    )
    entry_budget = max(2.0 * tick, PREFLIGHT_MAX_ENTRY_DRIFT_ATR * atr)
    if adverse_entry_drift > entry_budget:
        return False, (
            'Giá entry trôi bất lợi quá lease '
            f'({adverse_entry_drift:.4f} > {entry_budget:.4f})'
        )

    zone = float(signal.get('setup_zone', 0.0) or 0.0)
    kind = signal.get('setup_kind')
    if zone <= 0.0 or kind not in ('zone', 'breakout'):
        return True, 'PASS'
    directional_failure = (
        zone - current if bias == 'LONG' else current - zone
    )
    failure_atr = (
        PREFLIGHT_BREAKOUT_FAILURE_ATR
        if kind == 'breakout' else PREFLIGHT_ZONE_FAILURE_ATR
    )
    failure_budget = max(2.0 * tick, failure_atr * atr)
    if directional_failure > failure_budget:
        return False, (
            f'{kind.upper()} thất bại trước submit '
            f'({directional_failure:.4f} > {failure_budget:.4f})'
        )
    return True, 'PASS'


def preflight_signal(signal, state):
    if not getattr(state, 'system_ready', False) or not getattr(state, 'trading_enabled', False):
        return False, 'Hệ thống không READY'
    if not getattr(state, 'execution_in_flight', False):
        return False, 'Không còn execution claim'
    if (
        state.execution_setup_id != signal.get('setup_id')
        or int(state.execution_generation) != int(signal.get('setup_generation', 0))
    ):
        return False, 'Claim không khớp setup/generation'
    setup = _find_setup(state, signal.get('setup_id'), signal.get('setup_generation', 0))
    if setup is None or setup.get('state') != 'EXECUTING':
        return False, 'Setup đã hết hạn hoặc bị invalidated'
    if state.co_lenh_mo or state.vi_the_hien_tai.active or state.dang_xu_ly_dong_lenh:
        return False, 'Đã có vị thế hoặc thao tác lệnh khác'

    tick_size = float(
        (getattr(state, 'exchange_filters', {}) or {}).get('tick_size', 0.1)
        or 0.1
    )
    identity_ok, reason = _setup_identity_matches(signal, setup, tick_size)
    if not identity_ok:
        return False, reason

    snapshot = snapshot_mod.capture(state, setup)
    fresh, reason = snapshot_mod.freshness(snapshot)
    if not fresh:
        return False, reason
    vetoed, reason = veto_mod.kiem_tra_veto(snapshot, signal['bias'])
    if vetoed:
        veto_mod.remember_confirmed_flash(
            state, snapshot, signal['bias'],
            now=float(getattr(snapshot, 'snapshot_time', time.time()) or time.time()),
        )
        return False, reason

    created_mono = float(signal.get('created_mono', 0.0) or 0.0)
    lease_age = time.monotonic() - created_mono if created_mono > 0.0 else float('inf')
    if lease_age < 0.0 or lease_age > PREFLIGHT_DECISION_LEASE_SECONDS:
        return False, f'Decision lease hết hạn ({lease_age:.3f}s)'

    price_ok, reason = _price_drift_guard(signal, snapshot, tick_size)
    if not price_ok:
        return False, reason
    return True, 'PASS'


async def submit_entry_idempotent(api, state, signal, side, qty, kwargs):
    """POST một ID; mất response thì query order/position trước khi kết luận."""
    client_order_id = signal['client_order_id']
    
    import os
    import time
    if os.getenv('SMC_EXECUTION_MODE') == 'SHADOW_MAINNET':
        if time.time() - getattr(state, 'execution_price_time', 0.0) > 3.0:
            return None, 599, False
        entry_price = getattr(state, 'execution_best_ask' if side == 'LONG' else 'execution_best_bid', 0.0)
        mock_result = {
            'orderId': int(time.time() * 1000),
            'clientOrderId': client_order_id,
            'status': 'FILLED',
            'executedQty': qty,
            'avgPrice': entry_price,
            'side': side,
            'type': 'MARKET'
        }
        return mock_result, 200, False

    result, status = await api.new_order(
        'BTCUSDT', side, 'MARKET', qty, newOrderRespType='RESULT',
        newClientOrderId=client_order_id, **kwargs
    )
    if status != 599:
        return result, status, False

    for delay in (0.25, 0.5, 1.0):
        await asyncio.sleep(delay)
        queried, query_status = await api.query_order('BTCUSDT', client_order_id)
        if query_status == 200:
            return queried, 200, True
        exchange_position = await wait_for_exchange_position(state, api, signal['bias'])
        if exchange_position is not None:
            recovered = {
                'clientOrderId': client_order_id,
                'avgPrice': exchange_position.get('entryPrice', 0.0),
                'status': 'FILLED_RECOVERED_FROM_POSITION',
            }
            return recovered, 200, True
    return result, 599, False


def _passive_intent_ttl(signal):
    frozen = signal.get('passive_intent_ttl_seconds')
    if frozen is not None:
        return max(30.0, min(90.0, float(frozen)))
    score = signal.get('continuous_score', {}) or {}
    confidence = max(0.0, min(1.0, float(score.get('confidence', 0.0) or 0.0)))
    activation = max(0.0, min(1.0, float(score.get('activation', 0.0) or 0.0)))
    return PASSIVE_INTENT_BASE_SECONDS + (
        PASSIVE_INTENT_CONFIDENCE_SECONDS * confidence * activation
    )


def _passive_fill_toxicity(state, bias, score, now=None):
    """Estimate whether a maker fill is likely to arrive with adverse flow.

    Sources that share aggTrade are combined by maximum severity rather than
    added as independent votes.  The output is continuous and only shortens
    retention/reduces effective power; safety invalidation remains separate.
    """
    now = float(now if now is not None else time.time())
    sign = 1.0 if bias == 'LONG' else -1.0
    registry = getattr(state, 'adverse_flow_memory_by_bias', {}) or {}
    memory = dict(registry.get(bias) or {}) if isinstance(registry, dict) else {}
    age = max(0.0, now - float(memory.get('ts', 0.0) or 0.0))
    memory_strength = (
        max(0.0, min(1.0, float(memory.get('severity', 0.0) or 0.0)))
        * math.exp(-math.log(2.0) * age / 5.0)
        if memory and memory.get('blocked_bias') == bias else 0.0
    )

    flash = veto_mod._flash_flow_evidence(state, bias)
    dominance = max(0.0, min(
        1.0, (float(flash['adverse_share']) - 0.50) / 0.50
    ))
    materiality = max(0.0, min(
        1.0,
        float(flash['adverse_qty']) / max(float(flash['threshold_qty']), 1e-9),
    ))
    net_materiality = max(0.0, min(
        1.0,
        float(flash['net_adverse_qty']) / max(float(flash['net_floor_qty']), 1e-9),
    ))
    current_flow_pressure = dominance * materiality * net_materiality
    aggtrade_pressure = max(memory_strength, current_flow_pressure)

    momentum = dict(score.get('momentum_breakdown') or {})
    opposing_flow = max(0.0, -sign * float(momentum.get('flow', 0.0) or 0.0))
    opposing_acceptance = max(
        0.0, -sign * float(momentum.get('acceptance', 0.0) or 0.0)
    )
    opposing_path = max(0.65 * opposing_flow, 0.80 * opposing_acceptance)
    toxicity = max(aggtrade_pressure, opposing_path)
    return {
        'score': max(0.0, min(1.0, toxicity)),
        'adverse_memory': memory_strength,
        'current_flow_pressure': current_flow_pressure,
        'opposing_flow': opposing_flow,
        'opposing_acceptance': opposing_acceptance,
        'source_policy': 'MAX_DEPENDENCY_FAMILY_V1',
    }


def _passive_quote_should_wait(setup, order_open=False):
    """Do not advertise a fresh maker quote into a strongly toxic move.

    This does not hard-veto the opportunity.  The same lifecycle remains in
    WATCH/RETRY_WAIT and may quote as soon as adverse flow decays.  Existing
    orders are handled by the cancel path so the one-order invariant remains
    intact.
    """
    if order_open or not isinstance(setup, dict):
        return False
    toxicity = float(
        (setup.get('passive_toxicity') or {}).get('score', 0.0) or 0.0
    )
    effective_power = float(
        setup.get('passive_current_trade_power', 0.0) or 0.0
    )
    retention_floor = float(setup.get('passive_retention_floor', 0.0) or 0.0)
    return bool(
        toxicity >= PASSIVE_TOXICITY_HOLD_MIN
        and retention_floor > 0.0
        and effective_power < retention_floor
    )


def _passive_thesis_reason(state, signal, setup, now_mono):
    if setup is None or setup.get('state') != 'EXECUTING':
        return 'THESIS_INVALIDATED'
    # A maker quote remains exposed while Commander is intentionally unable to
    # claim another setup. Keep adverse-flow memory current during that window.
    veto_mod.remember_confirmed_flash(
        state, state, signal.get('bias'), now=time.time()
    )
    score = setup.get('passive_live_score') or signal.get('continuous_score') or {}
    if signal.get('score_version') == 'CONTINUOUS_V2':
        if float(score.get('impulse_conflict', 0.0) or 0.0) > 0.75:
            return 'OPPOSING_MOMENTUM_CONFLICT'
        if _opportunity_retention_enabled():
            policy = setup.get('retention_policy') or _retention_policy(signal)
            setup['retention_policy'] = policy
            threshold = float(policy['retention_floor'])
            grace = float(policy['below_floor_grace_seconds'])
        else:
            threshold = float(score.get('activation_floor', 100.0) or 100.0)
            grace = 2.0
        current_power = float(score.get('trade_power', 0.0) or 0.0)
        toxicity = _passive_fill_toxicity(
            state, signal.get('bias'), score, now=time.time()
        )
        toxicity_score = float(toxicity['score'])
        effective_power = current_power * (1.0 - 0.80 * toxicity_score)
        effective_grace = max(
            0.50, grace * max(0.01, (1.0 - 0.90 * toxicity_score) ** 2)
        )
        below = effective_power < threshold
        setup['passive_current_trade_power_raw'] = current_power
        setup['passive_current_trade_power'] = effective_power
        setup['passive_retention_floor'] = threshold
        setup['passive_toxicity'] = toxicity
        setup['passive_effective_grace_seconds'] = effective_grace
        if below:
            since = setup.setdefault('passive_power_below_since_mono', now_mono)
            setup['passive_below_floor_seconds'] = max(
                0.0, now_mono - float(since)
            )
            if setup['passive_below_floor_seconds'] >= effective_grace:
                return (
                    'PASSIVE_TOXICITY_EXPIRED'
                    if toxicity_score >= 0.25
                    else
                    'RETENTION_POWER_EXPIRED'
                    if _opportunity_retention_enabled()
                    else 'TRADE_POWER_BELOW_FLOOR_2S'
                )
        else:
            setup.pop('passive_power_below_since_mono', None)
            setup['passive_below_floor_seconds'] = 0.0
    if not getattr(state, 'system_ready', False) or not getattr(state, 'trading_enabled', False):
        return 'SYSTEM_NOT_READY'
    if time.time() - float(getattr(state, 'execution_price_time', 0.0) or 0.0) > 3.0:
        return 'EXECUTION_FEED_STALE'
    return None


def _passive_economics_reason(state, signal, qty, tick_size):
    if not _dynamic_path_enabled():
        return None
    current = state.best_ask if signal.get('bias') == 'LONG' else state.best_bid
    try:
        _, evaluated_entry, levels, _, plan, economic = _dynamic_bundle(
            state, signal, qty, current, tick_size,
            allow_split_sl=mainnet_safety.execution_venue() != 'MAINNET',
        )
    except Exception as exc:
        return f'ECONOMICS_RECHECK_ERROR:{type(exc).__name__}'
    geometry_ok, _ = risk.validate_level_geometry(
        levels, float(evaluated_entry), signal.get('bias'), tick_size,
        float(getattr(state, 'atr_1m', 0.0) or 0.0),
    )
    if not geometry_ok:
        return 'GEOMETRY_INVALIDATED'
    if mainnet_safety.execution_venue() == 'MAINNET':
        risk_plan = dict(levels.get('mainnet_risk_plan') or {})
        if not risk_plan:
            return 'MAINNET_RISK_PLAN_MISSING'
        if not risk_plan.get('eligible'):
            return (
                'MAINNET_RISK_BUDGET_INVALID'
                if risk_plan.get('reason') == 'SOFT_SL_OUTSIDE_SAFE_BUDGET'
                else 'MAINNET_RISK_PLAN_INVALID'
            )
        edge = float(economic.get('realizable_edge_lcb', -1e9) or -1e9)
        if edge < mainnet_safety.maker_min_edge_bps():
            return 'MAINNET_MAKER_EDGE_BELOW_BUFFER'
    if not economic.get('structural_fee_floor_pass', False):
        return 'REALIZABLE_EDGE_NEGATIVE'
    if str(plan.get('entry_policy') or economic.get('entry_policy') or '') == 'BLOCK':
        return 'REALIZABLE_EDGE_NEGATIVE'
    return None


def _market_conversion_assessment(state, signal, setup, qty, tick_size):
    """Price one taker fallback without using elapsed time as an entry signal."""
    result = {
        'eligible': False, 'reason': 'RETENTION_DISABLED',
        'market_edge_lcb': None, 'strategy_price': None,
        'execution_price': None, 'venue_basis_bps': None,
    }
    if not _opportunity_retention_enabled() or not _dynamic_path_enabled():
        return result
    if signal.get('_market_conversion_attempted'):
        result['reason'] = 'CONVERSION_ALREADY_ATTEMPTED'
        return result
    score = (setup or {}).get('passive_live_score') or signal.get('continuous_score') or {}
    policy = (setup or {}).get('retention_policy') or _retention_policy(signal)
    power = float(score.get('trade_power', 0.0) or 0.0)
    if power < float(policy['retention_floor']):
        result['reason'] = 'POWER_BELOW_RETENTION_FLOOR'
        return result
    if float(score.get('impulse_conflict', 0.0) or 0.0) > 0.75:
        result['reason'] = 'OPPOSING_MOMENTUM_CONFLICT'
        return result

    bias = signal.get('bias')
    strategy_price = float(
        state.best_ask if bias == 'LONG' else state.best_bid
    )
    decision_price = float(signal.get('decision_price', 0.0) or 0.0)
    favorable_progress = (
        strategy_price - decision_price
        if bias == 'LONG' else decision_price - strategy_price
    )
    if decision_price <= 0.0 or favorable_progress < 2.0 * float(tick_size):
        result.update({
            'reason': 'NO_FAVORABLE_MOVE_AWAY',
            'strategy_price': strategy_price,
            'favorable_progress': favorable_progress,
        })
        return result

    market_signal = dict(signal)
    market_signal['entry_style'] = 'MARKET_CHASE'
    try:
        conversion_fill, evaluated_entry, levels, split_policy, plan, economic = _dynamic_bundle(
            state, market_signal, qty, strategy_price, tick_size,
            allow_split_sl=mainnet_safety.execution_venue() != 'MAINNET',
        )
    except Exception as exc:
        result['reason'] = f'CONVERSION_ECONOMICS_ERROR:{type(exc).__name__}'
        return result
    edge = float(economic.get('realizable_edge_lcb', -1e9) or -1e9)
    entry_policy = str(plan.get('entry_policy') or economic.get('entry_policy') or '')
    execution_price = float(
        getattr(
            state,
            'execution_best_ask' if bias == 'LONG' else 'execution_best_bid',
            0.0,
        ) or 0.0
    )
    basis_bps = (
        (execution_price - strategy_price) / strategy_price * 10000.0
        if execution_price > 0.0 and strategy_price > 0.0 else None
    )
    result.update({
        'reason': 'PASS', 'market_edge_lcb': edge,
        'strategy_price': strategy_price, 'execution_price': execution_price,
        'venue_basis_bps': basis_bps, 'favorable_progress': favorable_progress,
        'entry_policy': entry_policy, 'exit_plan': plan,
        'economic': economic, 'levels': levels,
        'split_sl_policy': split_policy,
        'evaluated_entry': evaluated_entry,
        'entry_fill_estimate': conversion_fill,
        'current_trade_power': power,
        'retention_floor': float(policy['retention_floor']),
    })
    minimum_market_edge = (
        mainnet_safety.market_min_edge_bps()
        if mainnet_safety.execution_venue() == 'MAINNET' else 2.0
    )
    risk_plan = dict(levels.get('mainnet_risk_plan') or {})
    risk_eligible = bool(
        risk_plan.get('eligible')
        if mainnet_safety.execution_venue() == 'MAINNET' else True
    )
    result['minimum_market_edge_lcb'] = minimum_market_edge
    result['mainnet_risk_plan'] = risk_plan
    result['eligible'] = bool(
        edge >= minimum_market_edge
        and entry_policy == 'MARKET_OR_CONFIGURED'
        and execution_price > 0.0
        and risk_eligible
    )
    if not result['eligible']:
        result['reason'] = (
            'MARKET_EDGE_BELOW_BUFFER'
            if edge < minimum_market_edge
            else 'MARKET_POLICY_NOT_AUTHORIZED'
            if entry_policy != 'MARKET_OR_CONFIGURED'
            else 'EXECUTION_BBO_MISSING'
            if execution_price <= 0.0
            else 'MAINNET_MARKET_RISK_BUDGET_INVALID'
        )
    return result


async def _cancel_passive_order(api, signal, latest, fallback_order_id):
    order_id = (latest or {}).get('orderId') or fallback_order_id
    if order_id is None:
        return latest, 599
    _, cancel_status = await api.cancel_order('BTCUSDT', order_id)
    queried, query_status = await api.query_order(
        'BTCUSDT', (latest or {}).get('clientOrderId')
        or signal.get('_passive_child_client_id')
    )
    if query_status == 200:
        return queried, 200
    # A failed query after a failed/unknown cancel cannot authorize a second
    # post-only order: preserve the one-order invariant fail-closed.
    return latest, 599 if cancel_status not in (200, 201) else query_status


async def submit_passive_entry(api, state, signal, side, qty, kwargs, tick_size):
    """Maintain one maker intent for 30-90s; never synthesize HTTP 409."""
    requested = float(signal.get('passive_entry_price', 0.0) or 0.0)
    if requested <= 0.0:
        return {'reason': 'PASSIVE_ENTRY_PRICE_MISSING'}, 422, False
    setup = _find_setup(
        state, signal.get('setup_id'), signal.get('setup_generation', 0)
    )
    score = signal.get('continuous_score') or {}
    signal.setdefault('initial_trade_power', float(score.get('trade_power', 0.0) or 0.0))
    signal.setdefault('initial_floor', float(score.get('activation_floor', 0.0) or 0.0))
    if setup is not None:
        ttl_seconds = _passive_intent_ttl(signal)
        setup['passive_intent_active'] = True
        setup['passive_intent_state'] = 'OPEN'
        setup['passive_live_score'] = dict(signal.get('continuous_score') or {})
        if _opportunity_retention_enabled():
            setup['retention_policy'] = _retention_policy(signal)
            setup['market_conversion_attempted'] = False
        setup['expires_mono'] = max(
            float(setup.get('expires_mono', 0.0) or 0.0),
            time.monotonic() + ttl_seconds + 2.0,
        )
        if _opportunity_retention_enabled():
            journal_mod.record_decision_stage(state, 'OPPORTUNITY_RETENTION_STARTED', {
                'setup_id': signal.get('setup_id'),
                'opportunity_id': signal.get('opportunity_id'),
                'ttl_seconds': ttl_seconds,
                **setup['retention_policy'],
            }, cycle_id=signal.get('position_cycle_id'))
    started = time.monotonic()
    expires = started + _passive_intent_ttl(signal)
    last_reprice = 0.0
    last_live = None
    latest = None
    result = None
    child_index = 0
    recovered = False
    retry_marked = False
    order_open = False
    last_conversion_check = 0.0
    toxicity_hold_active = False

    while True:
        now_mono = time.monotonic()
        reason = _passive_thesis_reason(state, signal, setup, now_mono)
        if reason is None and now_mono >= expires:
            reason = 'EXPIRED_UNFILLED'
        toxicity_wait = bool(
            reason is None
            and _passive_quote_should_wait(setup, order_open=order_open)
        )
        if toxicity_wait:
            if not toxicity_hold_active:
                toxicity_hold_active = True
                if setup is not None:
                    setup['passive_intent_state'] = 'TOXICITY_HOLD'
                journal_mod.record_decision_stage(
                    state, 'PASSIVE_TOXICITY_HOLD', {
                        'setup_id': signal.get('setup_id'),
                        'opportunity_id': signal.get('opportunity_id'),
                        'toxicity': (
                            setup.get('passive_toxicity') if setup else None
                        ),
                        'effective_power': (
                            setup.get('passive_current_trade_power')
                            if setup else None
                        ),
                        'retention_floor': (
                            setup.get('passive_retention_floor')
                            if setup else None
                        ),
                        'order_open': False,
                    }, cycle_id=signal.get('position_cycle_id'),
                )
            await asyncio.sleep(0.20)
            continue
        if toxicity_hold_active and reason is None:
            toxicity_hold_active = False
            journal_mod.record_decision_stage(
                state, 'PASSIVE_TOXICITY_CLEARED', {
                    'setup_id': signal.get('setup_id'),
                    'opportunity_id': signal.get('opportunity_id'),
                    'toxicity': (
                        setup.get('passive_toxicity') if setup else None
                    ),
                }, cycle_id=signal.get('position_cycle_id'),
            )
        if reason is None and (not order_open or now_mono - last_reprice >= PASSIVE_REPRICE_MIN_SECONDS):
            reason = _passive_economics_reason(state, signal, qty, tick_size)

        if side == 'BUY':
            strategy_ref = float(state.best_bid)
            live = float(getattr(state, 'execution_best_bid', 0.0) or strategy_ref)
            translated = requested + (live - strategy_ref)
            desired = min(translated, live)
        else:
            strategy_ref = float(state.best_ask)
            live = float(getattr(state, 'execution_best_ask', 0.0) or strategy_ref)
            translated = requested + (live - strategy_ref)
            desired = max(translated, live)
        desired = risk.round_to_tick(desired, tick_size)
        should_reprice = bool(
            order_open and last_live is not None
            and now_mono - last_reprice >= PASSIVE_REPRICE_MIN_SECONDS
            and abs(desired - last_live) >= PASSIVE_REPRICE_TICKS * tick_size
            and child_index < PASSIVE_REPRICE_MAX
        )

        conversion = None
        if (
            reason is None and _opportunity_retention_enabled()
            and now_mono - last_conversion_check >= PASSIVE_REPRICE_MIN_SECONDS
        ):
            last_conversion_check = now_mono
            conversion = _market_conversion_assessment(
                state, signal, setup, qty, tick_size
            )
            if setup is not None:
                setup['last_market_conversion_assessment'] = {
                    key: value for key, value in conversion.items()
                    if key not in (
                        'economic', 'exit_plan', 'levels', 'split_sl_policy',
                        'entry_fill_estimate',
                    )
                }
            if conversion.get('eligible'):
                if order_open:
                    latest, cancel_status = await _cancel_passive_order(
                        api, signal, latest, (result or {}).get('orderId')
                    )
                    if cancel_status != 200:
                        if setup is not None:
                            setup['passive_intent_state'] = 'CANCEL_UNKNOWN'
                        return latest or {'reason': 'PASSIVE_CANCEL_UNKNOWN'}, 599, recovered
                    order_open = False
                    executed = float((latest or {}).get('executedQty', 0.0) or 0.0)
                    if executed > 0.0:
                        latest = dict(latest)
                        latest['status'] = 'PARTIALLY_FILLED_CANCELED'
                        if setup is not None:
                            setup['passive_intent_active'] = False
                            setup['passive_intent_state'] = 'PARTIAL_FILL'
                        return latest, 200, True
                signal['_market_conversion_attempted'] = True
                signal['entry_style'] = 'MARKET_CHASE'
                signal['exit_plan'] = dict(conversion['exit_plan'])
                signal['split_sl_policy'] = dict(conversion['split_sl_policy'])
                signal['_conversion_levels'] = dict(conversion['levels'])
                signal['_conversion_strategy_entry'] = float(
                    conversion['evaluated_entry']
                )
                signal['_conversion_entry_fill'] = dict(
                    conversion['entry_fill_estimate']
                )
                if setup is not None:
                    setup['market_conversion_attempted'] = True
                    setup['passive_intent_state'] = 'CONVERTING_TO_MARKET'
                journal_mod.record_decision_stage(state, 'PASSIVE_TO_MARKET_CONVERSION', {
                    'setup_id': signal.get('setup_id'),
                    'opportunity_id': signal.get('opportunity_id'),
                    'market_edge_lcb': conversion.get('market_edge_lcb'),
                    'minimum_market_edge_lcb': conversion.get(
                        'minimum_market_edge_lcb', 2.0
                    ),
                    'strategy_price': conversion.get('strategy_price'),
                    'execution_price': conversion.get('execution_price'),
                    'venue_basis_bps': conversion.get('venue_basis_bps'),
                    'favorable_progress': conversion.get('favorable_progress'),
                    'current_trade_power': conversion.get('current_trade_power'),
                    'retention_floor': conversion.get('retention_floor'),
                }, cycle_id=signal.get('position_cycle_id'))
                cycle = getattr(state, 'trade_cycles', {}).get(
                    signal.get('position_cycle_id')
                )
                if cycle is not None:
                    cycle['entry_style'] = 'MARKET_CHASE'
                    cycle['market_conversion_economic'] = dict(
                        conversion.get('economic') or {}
                    )
                    cycle['market_conversion_venue'] = {
                        'strategy_price': conversion.get('strategy_price'),
                        'execution_price': conversion.get('execution_price'),
                        'venue_basis_bps': conversion.get('venue_basis_bps'),
                    }
                converted, converted_status, converted_recovered = (
                    await submit_entry_idempotent(api, state, signal, side, qty, kwargs)
                )
                if setup is not None and converted_status == 200:
                    setup['passive_intent_active'] = False
                    setup['passive_intent_state'] = 'CONVERTED'
                return converted, converted_status, bool(recovered or converted_recovered)

        if reason is not None or should_reprice:
            if order_open:
                latest, cancel_status = await _cancel_passive_order(
                    api, signal, latest, (result or {}).get('orderId')
                )
                if cancel_status != 200:
                    if setup is not None:
                        setup['passive_intent_state'] = 'CANCEL_UNKNOWN'
                    return latest or {'reason': 'PASSIVE_CANCEL_UNKNOWN'}, 599, recovered
                order_open = False
                executed = float((latest or {}).get('executedQty', 0.0) or 0.0)
                if executed > 0.0:
                    latest = dict(latest)
                    latest['status'] = 'PARTIALLY_FILLED_CANCELED'
                    if setup is not None:
                        setup['passive_intent_active'] = False
                        setup['passive_intent_state'] = 'PARTIAL_FILL'
                    return latest, 200, True
            if reason is not None:
                if setup is not None:
                    setup['passive_intent_active'] = False
                    setup['passive_intent_state'] = reason
                return {
                    'reason': reason, 'order': latest,
                    'intent_age_seconds': max(0.0, now_mono - started),
                }, 204, recovered
            if setup is not None:
                setup['passive_intent_state'] = 'RETRY_WAIT'
            journal_mod.record_decision_stage(state, 'PASSIVE_RETRY_WAIT', {
                'setup_id': signal.get('setup_id'), 'reason': 'BBO_REPRICE',
                'previous_price': last_live, 'next_price': desired,
            }, cycle_id=signal.get('position_cycle_id'))

        if not order_open:
            child_index += 1
            child_id = forensic_order_id(
                state, 'PASSIVE',
                opportunity_id=signal.get('opportunity_id'),
                setup_id=signal.get('setup_id'),
                generation=signal.get('setup_generation', 0),
                nonce=child_index,
            )
            signal['_passive_child_client_id'] = child_id
            
            import os
            import time
            if os.getenv('SMC_EXECUTION_MODE') == 'SHADOW_MAINNET':
                if time.time() - getattr(state, 'execution_price_time', 0.0) > 3.0:
                    return None, 599, recovered
                entry_price = getattr(state, 'execution_best_ask' if side == 'LONG' else 'execution_best_bid', 0.0)
                mock_result = {
                    'orderId': int(time.time() * 1000),
                    'clientOrderId': child_id,
                    'status': 'FILLED',
                    'executedQty': qty,
                    'avgPrice': entry_price,
                    'price': desired,
                    'side': side,
                    'type': 'LIMIT'
                }
                result, status = mock_result, 200
            else:
                result, status = await api.new_order(
                    'BTCUSDT', side, 'LIMIT', qty, timeInForce='GTX', price=desired,
                    newOrderRespType='RESULT', newClientOrderId=child_id, **kwargs,
                )
            if status == 599:
                queried, query_status = await api.query_order('BTCUSDT', child_id)
                if query_status != 200:
                    return result, 599, recovered
                result, status, recovered = queried, 200, True
            if status != 200:
                return result, status, recovered
            latest = result
            order_open = True
            last_live = desired
            last_reprice = time.monotonic()
            if setup is not None:
                setup['passive_intent_state'] = 'OPEN'
            journal_mod.record_decision_stage(state, 'PASSIVE_ORDER_OPEN', {
                'setup_id': signal.get('setup_id'), 'child_client_order_id': child_id,
                'price': desired, 'reprice_index': child_index - 1,
                'expires_in_seconds': max(0.0, expires - last_reprice),
                'strategy_bbo_reference': strategy_ref,
                'execution_bbo_reference': live,
                'venue_basis_bps': (
                    (live - strategy_ref) / strategy_ref * 10000.0
                    if strategy_ref > 0.0 else None
                ),
                'current_trade_power': (
                    setup.get('passive_current_trade_power') if setup else None
                ),
                'retention_floor': (
                    setup.get('passive_retention_floor') if setup else None
                ),
            }, cycle_id=signal.get('position_cycle_id'))

        status_name = str((latest or {}).get('status', '')).upper()
        executed = float((latest or {}).get('executedQty', 0.0) or 0.0)
        if status_name == 'FILLED':
            if setup is not None:
                setup['passive_intent_active'] = False
                setup['passive_intent_state'] = 'FILLED'
            return latest, 200, recovered
        if executed > 0.0:
            latest, cancel_status = await _cancel_passive_order(
                api, signal, latest, (result or {}).get('orderId')
            )
            if cancel_status != 200:
                return latest, 599, True
            latest = dict(latest or {})
            latest['status'] = 'PARTIALLY_FILLED_CANCELED'
            if setup is not None:
                setup['passive_intent_active'] = False
                setup['passive_intent_state'] = 'PARTIAL_FILL'
            return latest, 200, True

        if not retry_marked and now_mono - started >= PASSIVE_RETRY_MARK_SECONDS:
            retry_marked = True
            if setup is not None:
                setup['passive_intent_state'] = 'RETRY_WAIT'
            journal_mod.record_decision_stage(state, 'PASSIVE_RETRY_WAIT', {
                'setup_id': signal.get('setup_id'),
                'reason': 'NOT_FILLED_AFTER_2S',
                'order_kept_open': True,
            }, cycle_id=signal.get('position_cycle_id'))

        await asyncio.sleep(0.20)
        queried, query_status = await api.query_order(
            'BTCUSDT', signal.get('_passive_child_client_id')
        )
        if query_status == 200:
            latest = queried


async def place_hard_sl(state, api, symbol='BTCUSDT'):
    position = state.vi_the_hien_tai
    close_side = 'SELL' if position.side == 'LONG' else 'BUY'
    last_result = None
    client_algo_id = position.hard_sl_client_algo_id or f"smc_sl_{int(time.time() * 1000)}"
    position.hard_sl_client_algo_id = client_algo_id
    tick_size = float((getattr(state, 'exchange_filters', {}) or {}).get(
        'tick_size', 0.1
    ) or 0.1)
    try:
        trigger_price = _canonical_stop_price(
            position.hard_sl, tick_size, position.side
        )
    except ValueError as exc:
        return False, {'code': 'LOCAL_INVALID_HARD_SL', 'msg': str(exc)}
    for attempt in range(5):
        params = {
            'symbol': symbol,
            'side': close_side,
            'type': 'STOP_MARKET',
            'triggerPrice': trigger_price,
            'workingType': 'MARK_PRICE',
            'closePosition': 'true',
            # Giữ nguyên ID qua mọi retry: response bị mất không được tạo SL trùng.
            'clientAlgoId': client_algo_id,
        }
        if getattr(state, 'account_hedge_mode', True):
            params['positionSide'] = position.side
        result, status = await api.new_algo_order(**params)
        last_result = result
        algo_id = result.get('algoId') if isinstance(result, dict) else None
        if status == 200 and algo_id is not None:
            position.hard_sl_algo_id = algo_id
            position.hard_sl_client_algo_id = result.get('clientAlgoId', client_algo_id)
            return True, result

        # Một POST có thể đã thành công nhưng response bị mất. Xác minh trước khi retry.
        open_algos, open_status = await api.get_open_algo_orders(symbol)
        recovered = (
            _find_algo_by_client_id(open_algos, client_algo_id)
            if open_status == 200 else None
        )
        if recovered is not None:
            position.hard_sl_algo_id = recovered.get('algoId')
            return True, recovered
        if not _retryable_algo_failure(status, result):
            logging.error(
                "❌ [HARD SL] Lỗi deterministic status=%s code=%s; "
                "không retry payload cũ.",
                status, result.get('code') if isinstance(result, dict) else None,
            )
            break
        if attempt < 4:
            await asyncio.sleep(0.2 * (attempt + 1))
    return False, last_result


async def vong_lap_thuc_thi(state, api, *legacy_args):
    engine = (
        dynamic_path_mod.VERSION if _dynamic_path_enabled()
        else 'STATIC_CAPTURE_V1'
    )
    logging.info(
        "🚀 [EXECUTOR] Đã khởi động; economic_engine=%s; lifecycle=%s; "
        "entry chỉ được giữ khi Hard SL được xác nhận.",
        engine, os.getenv('SMC_ENTRY_LIFECYCLE', 'LEGACY'),
    )
    journal_mod.record_decision_stage(state, 'ECONOMIC_ENGINE_READY', {
        'engine': engine,
        'entry_lifecycle': os.getenv('SMC_ENTRY_LIFECYCLE', 'LEGACY'),
        'model_version': (
            dynamic_path_mod.MODEL_VERSION if _dynamic_path_enabled() else None
        ),
        'strategy_venue': 'BINANCE_FUTURES_MAINNET',
        'execution_venue': 'BINANCE_FUTURES_TESTNET',
    })
    while True:
        if state.hang_doi_tin_hieu is None:
            await asyncio.sleep(0.1)
            continue
        signal = await state.hang_doi_tin_hieu.get()
        try:
            await xu_ly_tin_hieu(signal, state, api)
        except Exception as exc:
            logging.exception("❌ [EXECUTOR] Lỗi xử lý tín hiệu: %s", exc)
        finally:
            state.hang_doi_tin_hieu.task_done()


def weak_probe_plus_economic_evaluation(signal, quantity, economic):
    """Final gate for the real 2.5% tier, evaluated at its executable qty."""
    size_policy = signal.get('size_policy', {}) or {}
    candidate = bool(
        size_policy.get('weak_probe_plus_candidate')
        and size_policy.get('tier') == 'WEAK_PROBE_PLUS'
    )
    minimum_edge = float(
        size_policy.get('weak_probe_plus_min_edge_bps', 12.0) or 12.0
    )
    edge = economic.get('expected_net_edge_bps')
    qualified = bool(
        candidate and quantity > 0.0
        and economic.get('structural_fee_floor_pass')
        and edge is not None and float(edge) >= minimum_edge
    )
    return {
        'policy_version': 'WEAK_PROBE_PLUS_LIVE_V1',
        'candidate': candidate,
        'qualified': qualified,
        'evaluated_size_pct': float(signal.get('size_pct', 0.0) or 0.0),
        'evaluated_qty': float(quantity or 0.0),
        'minimum_expected_net_edge_bps': minimum_edge,
        'expected_net_edge_bps': edge,
        'fallback_tier': size_policy.get('fallback_tier'),
        'fallback_size_pct': size_policy.get('fallback_size_pct'),
        'qualification': size_policy.get('weak_probe_plus_qualification', {}),
    }


def weak_probe_economic_evaluation(signal, economic):
    """Them buffer nho cho tier 1-CORE; tier manh giu nguyen fee contract."""
    tier = str(((signal.get('size_policy') or {}).get('tier') or ''))
    edge = economic.get('expected_net_edge_bps')
    applies = tier == 'WEAK_PROBE'
    qualified = bool(
        not applies
        or (
            edge is not None
            and float(edge) >= WEAK_PROBE_MIN_EXPECTED_NET_EDGE_BPS
        )
    )
    return {
        'policy_version': 'WEAK_PROBE_EDGE_BUFFER_V1',
        'applies': applies,
        'qualified': qualified,
        'tier': tier,
        'minimum_expected_net_edge_bps': WEAK_PROBE_MIN_EXPECTED_NET_EDGE_BPS,
        'expected_net_edge_bps': edge,
    }


def continuous_economic_evaluation(signal, economic):
    """Continuous live scorer keeps a larger fee buffer for small probes."""
    applies = signal.get('score_version') in ('CONTINUOUS_V1', 'CONTINUOUS_V2')
    tier = str(((signal.get('size_policy') or {}).get('tier') or ''))
    size_pct = float(signal.get('size_pct', 0.0) or 0.0)
    minimum_edge = 6.0 if tier in ('WATCH', 'MICRO') or size_pct < 3.0 else 4.0
    edge = economic.get('expected_net_edge_bps')
    qualified = bool(
        not applies or (
            economic.get('structural_fee_floor_pass')
            and edge is not None and float(edge) >= minimum_edge
        )
    )
    return {
        'policy_version': 'CONTINUOUS_EDGE_BUFFER_V2',
        'applies': applies, 'qualified': qualified, 'tier': tier,
        'minimum_expected_net_edge_bps': minimum_edge,
        'expected_net_edge_bps': edge,
    }


def dynamic_entry_gate_evaluation(economic, mainnet_fixed=False):
    """Return one truthful reason for a Dynamic Path entry decision.

    Fee edge and Mainnet stop-budget eligibility are independent contracts.
    Combining them into one Boolean previously produced impossible logs such
    as ``27.3 bps < 2 bps`` and mislabeled risk waits as fee failures.
    """
    structural_pass = bool(economic.get('structural_fee_floor_pass'))
    edge = economic.get('realizable_edge_lcb')
    try:
        edge_value = float(edge)
    except (TypeError, ValueError):
        edge_value = -1e9
    if not math.isfinite(edge_value):
        edge_value = -1e9
    minimum_edge = (
        mainnet_safety.maker_min_edge_bps() if mainnet_fixed else 0.0
    )
    risk_plan = dict(economic.get('mainnet_risk_plan') or {})
    risk_reason = str(risk_plan.get('reason') or '')

    reason = 'PASS'
    retryable = False
    if not structural_pass:
        reason = 'REALIZABLE_EDGE_NEGATIVE'
        retryable = True
    elif edge_value < minimum_edge:
        reason = 'MAINNET_MAKER_EDGE_BELOW_BUFFER'
        retryable = bool(mainnet_fixed)
    elif mainnet_fixed and not risk_plan:
        reason = 'MAINNET_RISK_PLAN_MISSING'
    elif mainnet_fixed and not risk_plan.get('eligible'):
        reason = 'MAINNET_RISK_BUDGET_INVALID'
        retryable = risk_reason == 'SOFT_SL_OUTSIDE_SAFE_BUDGET'

    return {
        'policy_version': 'DYNAMIC_PATH_ENTRY_GATE_V4',
        'qualified': reason == 'PASS',
        'reason': reason,
        'retryable': retryable,
        'structural_fee_floor_pass': structural_pass,
        'maker_edge_pass': edge_value >= minimum_edge,
        'risk_budget_pass': (
            bool(risk_plan.get('eligible')) if mainnet_fixed else True
        ),
        'minimum_expected_net_edge_bps': minimum_edge,
        'expected_net_edge_bps': edge,
        'entry_policy': economic.get('entry_policy'),
        'mainnet_risk_plan': risk_plan,
        'risk_reason': risk_reason or None,
    }


async def xu_ly_tin_hieu(signal, state, api):
    """Contain the whole claimed path, including calculations before POST.

    The inner implementation already fail-closes an ambiguous confirmed POST.
    This outer boundary handles malformed depth/levels/journal failures that
    happen earlier, so one exception cannot leave execution_in_flight stuck.
    """
    try:
        return await _xu_ly_tin_hieu(signal, state, api)
    except Exception:
        same_claim = bool(
            getattr(state, 'execution_in_flight', False)
            and state.execution_setup_id == signal.get('setup_id')
            and int(getattr(state, 'execution_generation', 0))
            == int(signal.get('setup_generation', 0))
        )
        if same_claim and not getattr(state, 'execution_unknown', False):
            release_execution(state, signal, 'INVALIDATED')
        raise


async def _xu_ly_tin_hieu(signal, state, api):
    if time.monotonic() - signal.get('created_mono', 0.0) > 5.0:
        journal_mod.record_decision_stage(state, 'EXECUTION_PREFLIGHT', {
            'setup_id': signal.get('setup_id'), 'result': 'BLOCK',
            'reason': 'SIGNAL_EXPIRED',
        })
        logging.warning("⚠️ [EXECUTOR] Tín hiệu quá hạn, bỏ qua.")
        release_execution(state, signal, 'INVALIDATED')
        return False

    bias = signal.get('bias')
    if bias not in ('LONG', 'SHORT'):
        journal_mod.record_decision_stage(state, 'EXECUTION_PREFLIGHT', {
            'setup_id': signal.get('setup_id'), 'result': 'BLOCK',
            'reason': 'INVALID_BIAS',
        })
        release_execution(state, signal, 'INVALIDATED')
        return False
    preflight_ok, preflight_reason = preflight_signal(signal, state)
    if not preflight_ok:
        journal_mod.record_decision_stage(state, 'EXECUTION_PREFLIGHT', {
            'setup_id': signal.get('setup_id'), 'result': 'BLOCK',
            'reason': preflight_reason,
        })
        logging.warning("⚠️ [EXECUTOR] Pre-flight chặn entry: %s", preflight_reason)
        release_execution(state, signal, 'ARMED_WINDOW')
        return False
    journal_mod.record_decision_stage(state, 'EXECUTION_PREFLIGHT', {
        'setup_id': signal.get('setup_id'), 'result': 'PASS',
        'reason': preflight_reason,
    })

    current_price = state.best_ask if bias == 'LONG' else state.best_bid
    frozen_base = float(
        ((signal.get('size_policy') or {}).get('allocation_base_usdt', 0.0))
        or 0.0
    )
    live_base = float(state.balance_usdt or 0.0)
    allocation_base = min(
        value for value in (frozen_base, live_base) if value > 0.0
    ) if frozen_base > 0.0 and live_base > 0.0 else max(frozen_base, live_base)
    mainnet_fixed = mainnet_safety.is_mainnet(api)
    if mainnet_fixed:
        # Every Mainnet opportunity starts maker-first. A taker conversion is
        # separately re-priced with a larger conservative edge buffer.
        signal['entry_style'] = 'PASSIVE_RETEST'
        qty = mainnet_safety.fixed_quantity()
        venue_size = {
            'executable': True, 'reason': 'MAINNET_FIXED_QTY',
            'quantity': qty, 'allocation_unit': 'FIXED_BASE_ASSET_QTY',
            'fixed_qty_btc': qty,
        }
    else:
        venue_size = risk.quantity_feasibility(
            allocation_base, signal.get('size_pct', 0.0), current_price,
            state.exchange_filters,
        )
    qty = venue_size['quantity']
    if qty <= 0:
        journal_mod.record_decision_stage(state, 'RISK_SIZE_EVALUATED', {
            'setup_id': signal.get('setup_id'), 'result': 'WAIT', 'qty': qty,
            'venue_size_feasibility': venue_size,
        })
        logging.warning(
            "⏳ [EXECUTOR] Allocation chưa đạt venue minimum; giữ WATCH: %s",
            venue_size,
        )
        release_execution(state, signal, 'WATCH')
        return False
    journal_mod.record_decision_stage(state, 'RISK_SIZE_EVALUATED', {
        'setup_id': signal.get('setup_id'), 'result': 'PASS', 'qty': qty,
        'size_pct': signal.get('size_pct'), 'reference_price': current_price,
        'allocation_base_usdt': allocation_base,
        'venue_size_feasibility': venue_size,
    })

    # Mainnet is the strategy truth. Testnet is only the execution venue.
    entry_levels = state.asks_top_10 if bias == 'LONG' else state.bids_top_10
    tick_size = float(state.exchange_filters.get('tick_size', 0.1))
    dynamic_v2 = _dynamic_path_enabled()
    if dynamic_v2:
        (
            shadow_entry, shadow_price, shadow_levels, split_sl_policy,
            exit_plan, economic,
        ) = _dynamic_bundle(
            state, signal, qty, current_price, tick_size,
            allow_split_sl=not mainnet_fixed,
        )
        original_size_pct = float(signal.get('size_pct', 0.0) or 0.0)
        entry_policy = str(exit_plan.get('entry_policy') or 'BLOCK')
        repriced_for_passive = False
        multiplier = float(exit_plan.get('economic_size_multiplier', 0.0) or 0.0)
        adjusted_size_pct = original_size_pct * multiplier
        if entry_policy == 'PASSIVE_RETEST_ONLY':
            adjusted_size_pct = min(
                adjusted_size_pct,
                float(exit_plan.get('passive_size_cap_pct', 2.0) or 2.0),
            )
            signal['entry_style'] = 'PASSIVE_RETEST'
            zone = float(signal.get('setup_zone', 0.0) or 0.0)
            atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
            if bias == 'LONG':
                signal['passive_entry_price'] = (
                    zone if zone > 0.0 and current_price - zone <= 0.50 * atr
                    else float(state.best_bid)
                )
            else:
                signal['passive_entry_price'] = (
                    zone if zone > 0.0 and zone - current_price <= 0.50 * atr
                    else float(state.best_ask)
                )
        if mainnet_fixed and entry_policy != 'BLOCK':
            entry_policy = 'PASSIVE_RETEST_ONLY'
            exit_plan['entry_policy'] = entry_policy
            economic['entry_policy'] = entry_policy
            signal['entry_style'] = 'PASSIVE_RETEST'
            zone = float(signal.get('setup_zone', 0.0) or 0.0)
            atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
            if bias == 'LONG':
                signal['passive_entry_price'] = (
                    zone if zone > 0.0 and current_price - zone <= 0.50 * atr
                    else float(state.best_bid)
                )
            else:
                signal['passive_entry_price'] = (
                    zone if zone > 0.0 and zone - current_price <= 0.50 * atr
                    else float(state.best_ask)
                )
        if mainnet_fixed:
            # Mainnet BTCUSDT quantity step is 0.001.  Preserve the scorer and
            # economics decision, but never translate a tier into more size.
            adjusted_size_pct = original_size_pct
        adjusted_size_pct = min(original_size_pct, adjusted_size_pct, 9.0)
        if (
            economic.get('structural_fee_floor_pass')
            and adjusted_size_pct + 1e-9 < original_size_pct
        ):
            if _opportunity_retention_enabled() and signal.get('entry_style') == 'PASSIVE_RETEST':
                adjusted_venue = _retention_size_feasibility(
                    allocation_base, adjusted_size_pct, current_price,
                    state.exchange_filters,
                )
                if adjusted_venue.get('policy_minimum_applied'):
                    adjusted_size_pct = float(
                        adjusted_venue.get('effective_target_notional_pct', adjusted_size_pct)
                    )
            else:
                adjusted_venue = risk.quantity_feasibility(
                    allocation_base, adjusted_size_pct, current_price,
                    state.exchange_filters,
                )
            journal_mod.record_decision_stage(state, 'DYNAMIC_SIZE_EVALUATED', {
                'setup_id': signal.get('setup_id'),
                'original_size_pct': original_size_pct,
                'adjusted_size_pct': adjusted_size_pct,
                'entry_policy': entry_policy,
                'economic_size_multiplier': multiplier,
                'venue_size_feasibility': adjusted_venue,
            })
            if not adjusted_venue.get('executable'):
                logging.info(
                    "⏳ [DYNAMIC PATH] Edge dương nhưng size sau economics dưới filter: %s",
                    adjusted_venue,
                )
                release_execution(state, signal, 'WATCH')
                return False
            signal['size_pct'] = adjusted_size_pct
            signal['size_policy'] = dict(signal.get('size_policy', {}) or {})
            signal['size_policy'].update({
                'economic_engine': dynamic_path_mod.VERSION,
                'pre_economic_size_pct': original_size_pct,
                'economic_size_multiplier': multiplier,
                'size_pct': adjusted_size_pct,
            })
            qty = float(adjusted_venue['quantity'])
            (
                shadow_entry, shadow_price, shadow_levels, split_sl_policy,
                exit_plan, economic,
            ) = _dynamic_bundle(
                state, signal, qty, current_price, tick_size,
                allow_split_sl=not mainnet_fixed,
            )
            repriced_for_passive = entry_policy == 'PASSIVE_RETEST_ONLY'
            # A marginal plan is never silently promoted from passive to market
            # merely because smaller quantity reduced depth slippage.
            if entry_policy == 'PASSIVE_RETEST_ONLY':
                exit_plan['entry_policy'] = 'PASSIVE_RETEST_ONLY'
                economic['entry_policy'] = 'PASSIVE_RETEST_ONLY'
        if entry_policy == 'PASSIVE_RETEST_ONLY' and not repriced_for_passive:
            # GTX is maker-only, so evaluate the actual passive fee schedule.
            # The second evaluation may improve edge but may not promote the
            # already marginal plan back to market entry.
            (
                shadow_entry, shadow_price, shadow_levels, split_sl_policy,
                exit_plan, economic,
            ) = _dynamic_bundle(
                state, signal, qty, current_price, tick_size,
                allow_split_sl=not mainnet_fixed,
            )
            exit_plan['entry_policy'] = 'PASSIVE_RETEST_ONLY'
            economic['entry_policy'] = 'PASSIVE_RETEST_ONLY'
        signal['exit_plan'] = dict(exit_plan)
        signal['split_sl_policy'] = split_sl_policy
        plus_evaluation = {
            'policy_version': 'DYNAMIC_PATH_REPLACES_WEAK_PROBE_EDGE_TIERS',
            'candidate': False, 'qualified': bool(economic.get('economic_pass')),
            'evaluated_size_pct': float(signal.get('size_pct', 0.0) or 0.0),
            'evaluated_qty': qty,
        }
        weak_evaluation = dynamic_entry_gate_evaluation(
            economic, mainnet_fixed=mainnet_fixed,
        )
        weak_evaluation['applies'] = True
        journal_mod.record_decision_stage(state, 'DYNAMIC_PATH_EVALUATED', {
            'setup_id': signal.get('setup_id'),
            'result': (
                'PASS' if weak_evaluation['qualified'] else
                'WAIT' if weak_evaluation.get('retryable') else 'BLOCK'
            ),
            'reason': weak_evaluation.get('reason'),
            'exit_plan': exit_plan,
        })
        journal_mod.record_decision_stage(state, 'EXIT_PLAN_SELECTED', {
            'setup_id': signal.get('setup_id'),
            'tp1': exit_plan.get('tp1'),
            'tp1_allocation': exit_plan.get('tp1_allocation'),
            'runner_target': exit_plan.get('runner_target'),
            'entry_policy': exit_plan.get('entry_policy'),
        })
    else:
        shadow_entry = economic_mod.estimate_market_fill(entry_levels, qty)
        shadow_entry['captured_at'] = time.time()
        shadow_price = float(shadow_entry.get('avg_price', 0.0) or current_price)
        shadow_levels = risk.calculate_levels(
            state, shadow_price, bias, tick_size, signal.get('mode', ''),
            setup_zone=signal.get('setup_zone'),
            setup_kind=signal.get('setup_kind'),
            breakout_target=signal.get('breakout_target'),
            breakout_target2=signal.get('breakout_target2'),
            breakout_target_basis=signal.get('breakout_target_basis'),
        )
        split_sl_policy = (
            {'enabled': False, 'version': 'MAINNET_FULL_EXIT_V1'}
            if mainnet_fixed else risk.build_value_area_split_sl_policy(
                bias, shadow_price, signal.get('decision_poc'),
                signal.get('decision_vah'), signal.get('decision_val'),
                signal.get('score_poc_modifier'),
                float(getattr(state, 'atr_1m', 0.0) or 0.0), tick_size,
                shadow_levels,
            )
        )
        signal['split_sl_policy'] = split_sl_policy
        if split_sl_policy['enabled']:
            shadow_levels['standard_hard_sl'] = shadow_levels['hard_sl']
            shadow_levels['hard_sl'] = split_sl_policy['sl2']
        economic = economic_mod.observe(
            state, bias, qty, shadow_levels['soft_tp1'],
            setup_kind=signal.get('setup_kind'),
            target_basis=shadow_levels.get('target_basis'),
            entry_style=signal.get('entry_style'),
        )
        plus_evaluation = weak_probe_plus_economic_evaluation(signal, qty, economic)
    if (
        not mainnet_fixed and not dynamic_v2
        and plus_evaluation['candidate'] and not plus_evaluation['qualified']
    ):
        # Giữ lệnh hợp lệ nhưng hạ tiền về WEAK_PROBE khi economics 2.5% không
        # đủ rộng. Recompute toàn bộ depth/levels/economics ở quantity thật 2%.
        signal['size_pct'] = 2.0
        signal['size_policy'] = dict(signal.get('size_policy', {}) or {})
        signal['size_policy'].update({
            'tier': 'WEAK_PROBE', 'size_pct': 2.0,
            'downgraded_from': 'WEAK_PROBE_PLUS',
            'downgrade_reason': 'EXPECTED_NET_EDGE_BELOW_12_BPS_AT_2_5_PERCENT',
        })
        qty = risk.calculate_qty(
            state.balance_usdt, 2.0, current_price, state.exchange_filters
        )
        journal_mod.record_decision_stage(state, 'WEAK_PROBE_PLUS_DOWNGRADED', {
            'setup_id': signal.get('setup_id'), 'qty': qty,
            'size_pct': 2.0, 'evaluation': plus_evaluation,
        })
        shadow_entry = economic_mod.estimate_market_fill(entry_levels, qty)
        shadow_entry['captured_at'] = time.time()
        shadow_price = float(shadow_entry.get('avg_price', 0.0) or current_price)
        shadow_levels = risk.calculate_levels(
            state, shadow_price, bias, tick_size, signal.get('mode', ''),
            setup_zone=signal.get('setup_zone'),
            setup_kind=signal.get('setup_kind'),
            breakout_target=signal.get('breakout_target'),
            breakout_target2=signal.get('breakout_target2'),
            breakout_target_basis=signal.get('breakout_target_basis'),
        )
        split_sl_policy = risk.build_value_area_split_sl_policy(
            bias,
            shadow_price,
            signal.get('decision_poc'),
            signal.get('decision_vah'),
            signal.get('decision_val'),
            signal.get('score_poc_modifier'),
            float(getattr(state, 'atr_1m', 0.0) or 0.0),
            tick_size,
            shadow_levels,
        )
        signal['split_sl_policy'] = split_sl_policy
        if split_sl_policy['enabled']:
            shadow_levels['standard_hard_sl'] = shadow_levels['hard_sl']
            shadow_levels['hard_sl'] = split_sl_policy['sl2']
        economic = economic_mod.observe(
            state, bias, qty, shadow_levels['soft_tp1'],
            setup_kind=signal.get('setup_kind'),
            target_basis=shadow_levels.get('target_basis'),
            entry_style=signal.get('entry_style'),
        )
        plus_evaluation['executed_tier'] = 'WEAK_PROBE'
        plus_evaluation['executed_size_pct'] = 2.0
        plus_evaluation['fallback_economic'] = economic
    elif not mainnet_fixed and not dynamic_v2 and plus_evaluation['qualified']:
        plus_evaluation['executed_tier'] = 'WEAK_PROBE_PLUS'
        plus_evaluation['executed_size_pct'] = 2.5
    signal['weak_probe_plus_evaluation'] = plus_evaluation
    if not dynamic_v2:
        weak_evaluation = weak_probe_economic_evaluation(signal, economic)
        continuous_evaluation = continuous_economic_evaluation(signal, economic)
        if continuous_evaluation['applies']:
            weak_evaluation = continuous_evaluation
    signal['weak_probe_economic_evaluation'] = weak_evaluation
    if _opportunity_retention_enabled() and signal.get('entry_style') == 'PASSIVE_RETEST':
        continuous_score = signal.get('continuous_score') or {}
        signal['initial_trade_power'] = float(
            continuous_score.get('trade_power', 0.0) or 0.0
        )
        signal['initial_floor'] = float(
            continuous_score.get('activation_floor', 0.0) or 0.0
        )
        signal['initial_edge_lcb'] = economic.get('realizable_edge_lcb')
        signal['entry_lifecycle_version'] = 'OPPORTUNITY_RETENTION_V1'
    setup = _find_setup(
        state, signal.get('setup_id'), signal.get('setup_generation', 0)
    )
    cycle_id = _create_or_reuse_cycle(
        state, signal, setup, qty, current_price, economic
    )
    signal['position_cycle_id'] = cycle_id
    if setup is not None:
        setup['position_cycle_id'] = cycle_id
        setup['last_economic_observation'] = economic
    logging.info(
        "💰 [ECONOMIC] cycle=%s enforced_net_edge=%s execution_floor=%s "
        "projected_pass=%s raw_tp1=%s projected=%s exec_required=%s cost=%s edge=%s",
        cycle_id, economic.get('structural_fee_floor_pass'),
        economic.get('execution_floor_pass'), economic.get('economic_pass'),
        economic.get('tp1_distance_bps'), economic.get('projected_capture_bps'),
        economic.get('execution_floor_required_bps'),
        economic.get('all_in_cost_bps'), economic.get('expected_net_edge_bps'),
    )
    entry_gate_qualified = bool(
        economic.get('structural_fee_floor_pass', False)
        and weak_evaluation['qualified']
    )
    gate_reason = str(
        weak_evaluation.get('reason')
        or economic.get('structural_fee_floor_reason')
        or (
            'ENTRY_GATE_PASSED'
            if entry_gate_qualified else 'STRUCTURAL_FEE_FLOOR_BLOCKED'
        )
    )
    journal_mod.record_decision_stage(state, 'WEAK_PROBE_ECONOMIC_EVALUATED', {
        'setup_id': signal.get('setup_id'),
        'result': (
            'PASS' if entry_gate_qualified else
            'WAIT' if weak_evaluation.get('retryable') else 'BLOCK'
        ),
        'reason': gate_reason,
        'evaluation': weak_evaluation,
    }, cycle_id=cycle_id)
    if not entry_gate_qualified:
        retryable_gate = bool(
            mainnet_fixed
            and strategy_profile.aug13_early_hybrid_enabled()
            and weak_evaluation.get('retryable')
        )
        cycle = getattr(state, 'trade_cycles', {}).get(cycle_id)
        if cycle is not None:
            cycle['entry_gate_evaluation'] = dict(weak_evaluation)

        if gate_reason == 'MAINNET_RISK_BUDGET_INVALID':
            risk_plan = dict(weak_evaluation.get('mainnet_risk_plan') or {})
            logging.warning(
                "🛡️ [RISK WAIT] cycle=%s reason=%s planned=%s max=%s "
                "soft_sl=%s bounded_hard_sl=%s",
                cycle_id, risk_plan.get('reason'),
                risk_plan.get('planned_worst_loss_usdt'),
                risk_plan.get('max_planned_loss_usdt'),
                risk_plan.get('soft_sl'), risk_plan.get('bounded_hard_sl'),
            )
        elif gate_reason == 'MAINNET_MAKER_EDGE_BELOW_BUFFER':
            logging.info(
                "⏳ [EDGE WAIT] cycle=%s edge=%s bps < maker_buffer=%s bps",
                cycle_id, weak_evaluation.get('expected_net_edge_bps'),
                weak_evaluation.get('minimum_expected_net_edge_bps'),
            )
        elif gate_reason == 'REALIZABLE_EDGE_NEGATIVE':
            logging.info(
                "⏳ [PATH WAIT] cycle=%s reason=%s edge=%s bps",
                cycle_id, economic.get('structural_fee_floor_reason'),
                weak_evaluation.get('expected_net_edge_bps'),
            )
        elif gate_reason in {
            'MAINNET_RISK_PLAN_MISSING', 'MAINNET_RISK_PLAN_INVALID',
        }:
            logging.error(
                "⛔ [ENTRY GATE] cycle=%s reason=%s evaluation=%s",
                cycle_id, gate_reason, weak_evaluation,
            )
        else:
            logging.info(
                "🛑 [ENTRY BLOCK] cycle=%s reason=%s",
                cycle_id, gate_reason,
            )

        if retryable_gate:
            journal_mod.record_decision_stage(state, 'ENTRY_GATE_RETRY_WAIT', {
                'setup_id': signal.get('setup_id'),
                'opportunity_id': signal.get('opportunity_id'),
                'reason': gate_reason,
                'evaluation': weak_evaluation,
            }, cycle_id=cycle_id)
            _mark_cycle_retry_wait(
                state, signal, setup, cycle_id, gate_reason,
            )
            release_execution(state, signal, 'WATCH')
            return False

        abort_reason = (
            gate_reason if mainnet_fixed else
            'WEAK_PROBE_EDGE_BLOCKED'
            if economic.get('structural_fee_floor_pass', False)
            else 'STRUCTURAL_FEE_FLOOR_BLOCKED'
        )
        journal_mod.abort_cycle(state, cycle_id, abort_reason)
        shadow_geometry_ok, shadow_geometry_reason = risk.validate_level_geometry(
            shadow_levels, shadow_price, bias, tick_size,
            float(getattr(state, 'atr_1m', 0.0) or 0.0),
        )
        journal_mod.record_decision_stage(state, 'FEE_BLOCKED_SHADOW_ELIGIBILITY', {
            'setup_id': signal.get('setup_id'),
            'result': 'PASS' if shadow_geometry_ok else 'BLOCK',
            'reason': shadow_geometry_reason,
            'semantic_key': setup.get('semantic_key') if setup else None,
        }, cycle_id=cycle_id)
        if shadow_geometry_ok and not mainnet_fixed and os.getenv(
            'SMC_MINIMAL_MAINNET_AUDIT', 'false'
        ).lower() not in ('1', 'true', 'yes', 'on'):
            journal_mod.activate_fee_blocked_shadow(
                state, cycle_id, signal, qty, shadow_entry,
                semantic_key=setup.get('semantic_key') if setup else None,
            )
        else:
            state.trade_cycles[cycle_id]['shadow'].update({
                'status': 'INELIGIBLE_DOWNSTREAM_GEOMETRY',
                'valid_for_strategy_evaluation': False,
                'invalid_reason': shadow_geometry_reason,
                'shadow_kind': 'FEE_BLOCKED',
            })
        release_execution(state, signal, 'INVALIDATED')
        return False

    geometry_ok, geometry_reason = risk.validate_level_geometry(
        shadow_levels, shadow_price, bias, tick_size,
        float(getattr(state, 'atr_1m', 0.0) or 0.0),
    )
    journal_mod.record_decision_stage(state, 'LEVEL_GEOMETRY_EVALUATED', {
        'setup_id': signal.get('setup_id'),
        'result': 'PASS' if geometry_ok else 'BLOCK',
        'reason': geometry_reason,
        'entry_price': shadow_price,
        'levels': dict(shadow_levels),
        'venue': 'BINANCE_FUTURES_MAINNET',
    })
    if not geometry_ok:
        logging.error(
            "⛔ [LEVEL GEOMETRY] Chặn entry %s @ %.2f: %s | levels=%s",
            bias, shadow_price, geometry_reason, shadow_levels,
        )
        journal_mod.abort_cycle(
            state, cycle_id, f'INVALID_LEVEL_GEOMETRY:{geometry_reason}'
        )
        release_execution(state, signal, 'INVALIDATED')
        return False

    if mainnet_fixed:
        risk_plan = dict(economic.get('mainnet_risk_plan') or {})
        gate_ok, gate_reason, gate_details = await mainnet_safety.exchange_entry_gate(
            api, state, shadow_price, shadow_levels['hard_sl'], risk_plan
        )
        journal_mod.record_decision_stage(state, 'MAINNET_ACCOUNT_PREFLIGHT', {
            'setup_id': signal.get('setup_id'),
            'result': 'PASS' if gate_ok else 'BLOCK',
            'reason': gate_reason, 'details': gate_details,
        }, cycle_id=cycle_id)
        if not gate_ok:
            journal_mod.abort_cycle(state, cycle_id, gate_reason)
            release_execution(state, signal, 'WATCH')
            return False
        qty = mainnet_safety.fixed_quantity()
        signal['split_sl_policy'] = {'enabled': False, 'version': 'MAINNET_FULL_EXIT_V1'}
        split_sl_policy = signal['split_sl_policy']
        signal['exit_plan'] = dict(signal.get('exit_plan') or {})
        signal['exit_plan']['tp1_allocation'] = 0.0

    state.dang_xu_ly_dong_lenh = True
    order_confirmed = False
    try:
        side = 'BUY' if bias == 'LONG' else 'SELL'
        kwargs = order_mode_kwargs(state, bias)
        entry_style = signal.get('entry_style')
        if signal.get('setup_kind') == 'breakout' and entry_style not in (
            'PASSIVE_RETEST', 'MARKET_CHASE',
        ):
            result, status, recovered = ({
                'reason': 'BREAKOUT_ENTRY_STYLE_NOT_READY'
            }, 422, False)
        elif entry_style == 'PASSIVE_RETEST':
            result, status, recovered = await submit_passive_entry(
                api, state, signal, side, qty, kwargs, tick_size
            )
        else:
            result, status, recovered = await submit_entry_idempotent(
                api, state, signal, side, qty, kwargs
            )
        if status == 599:
            cycle = state.trade_cycles.get(cycle_id)
            if cycle is not None:
                cycle['status'] = 'ENTRY_UNKNOWN'
            state.execution_unknown = True
            state.execution_unknown_since = time.time()
            state.system_ready = False
            state.trading_enabled = False
            state.last_readiness_reason = (
                f"Entry {signal['client_order_id']} chưa xác minh; khóa fail-closed"
            )
            logging.critical(
                "⛔ [EXECUTOR] Entry timeout chưa xác minh; giữ EXECUTING và khóa entry: %s",
                signal['client_order_id'],
            )
            return False
        if status == 204 and entry_style == 'PASSIVE_RETEST':
            reason = str((result or {}).get('reason') or 'PASSIVE_INTENT_ENDED')
            journal_mod.record_decision_stage(state, 'PASSIVE_INTENT_TERMINATED', {
                'setup_id': signal.get('setup_id'), 'reason': reason,
                'intent_age_seconds': (result or {}).get('intent_age_seconds'),
                'result': 'EXPIRED' if reason == 'EXPIRED_UNFILLED' else 'CANCELLED',
                'current_trade_power': (
                    setup.get('passive_current_trade_power') if setup else None
                ),
                'retention_floor': (
                    setup.get('passive_retention_floor') if setup else None
                ),
                'below_floor_seconds': (
                    setup.get('passive_below_floor_seconds') if setup else None
                ),
                'passive_toxicity': (
                    setup.get('passive_toxicity') if setup else None
                ),
                'effective_grace_seconds': (
                    setup.get('passive_effective_grace_seconds') if setup else None
                ),
            }, cycle_id=cycle_id)
            aug13_retry = bool(
                strategy_profile.aug13_early_hybrid_enabled()
                and strategy_profile.passive_reason_is_retryable(reason)
            )
            legacy_retry = bool(
                not _opportunity_retention_enabled()
                and reason in (
                    'TRADE_POWER_BELOW_FLOOR_2S', 'REALIZABLE_EDGE_NEGATIVE',
                )
            )
            if aug13_retry:
                _mark_cycle_retry_wait(
                    state, signal, setup, cycle_id, reason
                )
                release_execution(state, signal, 'WATCH')
                return False

            journal_mod.abort_cycle(state, cycle_id, reason)
            if (
                _opportunity_retention_enabled()
                and not strategy_profile.aug13_early_hybrid_enabled()
            ) or strategy_profile.passive_reason_is_structural_terminal(reason):
                _mark_intent_terminal(state, signal, reason)
            _clear_retry_cycle(state, signal, setup)
            release_execution(
                state, signal,
                'WATCH' if legacy_retry else 'EXPIRED'
                if reason == 'EXPIRED_UNFILLED' else 'INVALIDATED',
            )
            return False
        if status != 200:
            logging.error("❌ [EXECUTOR] Entry thất bại: %s", result)
            journal_mod.abort_cycle(state, cycle_id, f"ENTRY_REJECTED:{status}")
            release_execution(state, signal, 'INVALIDATED')
            return False
        if signal.get('_market_conversion_attempted'):
            shadow_levels = dict(signal.get('_conversion_levels') or shadow_levels)
            shadow_price = float(
                signal.get('_conversion_strategy_entry', shadow_price) or shadow_price
            )
            shadow_entry = dict(
                signal.get('_conversion_entry_fill') or shadow_entry
            )
            split_sl_policy = dict(
                signal.get('split_sl_policy') or split_sl_policy
            )
        filled_qty = float(result.get('executedQty', 0.0) or 0.0)
        if filled_qty > 0.0:
            qty = min(qty, filled_qty)
        order_confirmed = True
        journal_mod.record_actual_order(
            state, cycle_id, 'ENTRY', result, qty,
            (
                float(getattr(state, 'execution_best_ask', 0.0) or 0.0)
                if bias == 'LONG'
                else float(getattr(state, 'execution_best_bid', 0.0) or 0.0)
            ),
            reason=(
                'OPPORTUNITY_RETENTION_MARKET_CONVERSION'
                if signal.get('_market_conversion_attempted')
                else 'CORE_SCORE_PASS'
            ),
            strategy_reference_price=current_price,
            execution_reference_price=(
                float(getattr(state, 'execution_best_ask', 0.0) or 0.0)
                if bias == 'LONG'
                else float(getattr(state, 'execution_best_bid', 0.0) or 0.0)
            ),
        )

        # Binance đôi khi trả entry thành công trước khi Algo engine thấy position.
        exchange_position = await wait_for_exchange_position(state, api, bias)
        avg_price = float(result.get('avgPrice', 0.0) or 0.0)
        if exchange_position is not None:
            avg_price = float(exchange_position.get('entryPrice', 0.0) or avg_price)
        if avg_price <= 0:
            avg_price = current_price
        # Chỉ Hard SL được dịch sang venue execution để POST lên Testnet.
        # Soft SL/TP và mọi quyết định Guardian giữ nguyên hệ quy chiếu Mainnet.
        execution_levels = (
            risk.translate_levels(shadow_levels, shadow_price, avg_price, tick_size)
            if getattr(api, 'testnet', False)
            else dict(shadow_levels)
        )
        actual_risk_plan = dict(
            execution_levels.get('mainnet_risk_plan') or {}
        )
        if mainnet_fixed:
            execution_bid = float(
                getattr(state, 'execution_best_bid', 0.0)
                or getattr(state, 'best_bid', 0.0) or 0.0
            )
            execution_ask = float(
                getattr(state, 'execution_best_ask', 0.0)
                or getattr(state, 'best_ask', 0.0) or 0.0
            )
            execution_mid = (
                (execution_bid + execution_ask) / 2.0
                if min(execution_bid, execution_ask) > 0.0 else avg_price
            )
            spread_bps = (
                max(0.0, execution_ask - execution_bid) / execution_mid * 10000.0
                if execution_mid > 0.0 else 0.0
            )
            entry_fee_bps = (
                economic_mod.ENTRY_FEE_BPS
                if signal.get('_market_conversion_attempted')
                else economic_mod.PASSIVE_ENTRY_FEE_BPS
            )
            execution_levels, actual_risk_plan = (
                mainnet_safety.apply_mainnet_loss_budget(
                    execution_levels, bias, avg_price, qty,
                    float(getattr(state, 'atr_1m', 0.0) or 0.0), tick_size,
                    spread_bps, entry_fee_bps, economic_mod.EXIT_FEE_BPS,
                    float(
                        shadow_entry.get('slippage_bps', 0.0) or 0.0
                    ) if signal.get('_market_conversion_attempted') else 0.0,
                    float((economic.get('exit_fill_estimate') or {}).get(
                        'slippage_bps', 0.0
                    ) or 0.0),
                )
            )
            execution_levels['mainnet_risk_plan'] = dict(actual_risk_plan)
            if not actual_risk_plan.get('eligible'):
                logging.critical(
                    "⛔ [MAINNET RISK] Fill không còn đặt được Hard SL trong "
                    "ngân sách %.4f USDT: %s",
                    mainnet_safety.max_planned_loss_usdt(), actual_risk_plan,
                )
                emergency = state.vi_the_hien_tai
                emergency.active = True
                emergency.side = bias
                emergency.qty = qty
                emergency.initial_qty = qty
                emergency.execution_entry_price = avg_price
                emergency.entry_price = shadow_price
                emergency.position_cycle_id = cycle_id
                emergency.hard_sl_algo_id = None
                emergency.mainnet_risk_plan = dict(actual_risk_plan)
                state.co_lenh_mo = True
                await guardian.close_position(
                    api, 'BTCUSDT', bias, qty, state,
                    reason='INVALID_ACTUAL_FILL_RISK_BUDGET',
                )
                journal_mod.abort_cycle(
                    state, cycle_id, 'INVALID_ACTUAL_FILL_RISK_BUDGET'
                )
                release_execution(state, signal, 'INVALIDATED')
                return False
            shadow_levels['hard_sl'] = execution_levels['hard_sl']
            shadow_levels['mainnet_risk_plan'] = dict(actual_risk_plan)
            economic['mainnet_risk_plan'] = dict(actual_risk_plan)
        execution_geometry_ok, execution_geometry_reason = risk.validate_level_geometry(
            execution_levels, avg_price, bias, tick_size,
            float(getattr(state, 'atr_1m', 0.0) or 0.0),
        )
        if not execution_geometry_ok:
            logging.critical(
                "⛔ [LEVEL GEOMETRY] Venue execution sai sau fill: %s",
                execution_geometry_reason,
            )
            await guardian.close_position(
                api, 'BTCUSDT', bias, qty, state,
                reason='INVALID_EXECUTION_LEVEL_GEOMETRY',
            )
            journal_mod.abort_cycle(
                state, cycle_id,
                f'INVALID_EXECUTION_LEVEL_GEOMETRY:{execution_geometry_reason}',
            )
            release_execution(state, signal, 'INVALIDATED')
            return False
        position = state.vi_the_hien_tai
        position.active = True
        position.side = bias
        position.qty = qty
        position.initial_qty = qty
        position.protection_closed_qty = 0.0
        position.protection_reasons_done = []
        position.entry_price = shadow_price
        position.strategy_entry_price = shadow_price
        position.execution_entry_price = avg_price
        position.hard_sl = execution_levels['hard_sl']
        position.strategy_hard_sl = shadow_levels['hard_sl']
        position.split_sl_enabled = bool(split_sl_policy.get('enabled'))
        position.split_sl1_done = False
        position.split_sl1_fraction = float(
            split_sl_policy.get('sl1_close_fraction', 0.90) or 0.90
        )
        position.split_sl1 = float(shadow_levels['soft_sl'])
        position.split_sl2 = float(shadow_levels['hard_sl'])
        position.standard_hard_sl = float(
            split_sl_policy.get('standard_hard_sl', shadow_levels['hard_sl'])
            or shadow_levels['hard_sl']
        )
        position.soft_sl = shadow_levels['soft_sl']
        position.soft_tp1 = shadow_levels['soft_tp1']
        position.soft_tp2 = shadow_levels['soft_tp2']
        position.tp1_allocation = max(
            0.0, min(0.70, float((signal.get('exit_plan') or {}).get(
                'tp1_allocation', 0.50
            ) or 0.0))
        )
        position.tp1_checkpoint_monetizable = bool(
            (signal.get('exit_plan') or {}).get('checkpoint_monetizable', False)
        )
        position.tp1_checkpoint_lock_net_bps = float(
            (signal.get('exit_plan') or {}).get('checkpoint_lock_net_bps', 0.0)
            or 0.0
        )
        position.runner_policy = str(
            (signal.get('exit_plan') or {}).get('runner_policy')
            or 'LEGACY_TP2'
        )
        position.guardian_policy = dict(shadow_levels.get('guardian_policy', {}) or {})
        position.strategy_profile = strategy_profile.current_profile()
        position.entry_continuous_score = dict(
            signal.get('continuous_score') or {}
        )
        position.dynamic_exit_plan = dict(signal.get('exit_plan') or {})
        position.breakout_target = signal.get('breakout_target')
        position.breakout_target2 = signal.get('breakout_target2')
        position.opened_at = time.time()
        position.mode = signal.get('mode', '')
        position.setup_id = signal.get('setup_id', '')
        position.setup_semantic_key = (
            setup.get('semantic_key', '') if setup is not None else ''
        )
        position.opportunity_id = str(
            signal.get('opportunity_id')
            or position.setup_semantic_key
            or ''
        )
        position.setup_zone = float(
            setup.get('zone', 0.0) if setup is not None else 0.0
        )
        position.venue_price_offset = (
            avg_price - shadow_price if getattr(api, 'testnet', False) else 0.0
        )
        position.setup_generation = int(signal.get('setup_generation', 0))
        position.position_cycle_id = cycle_id
        position.entry_order_id = result.get('orderId') if isinstance(result, dict) else None
        position.entry_client_order_id = signal.get('client_order_id')
        position.mainnet_risk_plan = dict(actual_risk_plan)
        position.tp1_done = False
        position.trailing_active = False
        position.add_on_done = False
        position.add_on_attempted = False
        position.tp2_extended = False
        position.shark_adverse_since = 0.0
        position.shark_support_since = 0.0
        state.co_lenh_mo = True
        state.last_signal_time = position.opened_at

        sl_ok, sl_result = await place_hard_sl(state, api)
        if not sl_ok:
            logging.critical("❌ [EXECUTOR] Hard SL thất bại, đóng vị thế ngay: %s", sl_result)
            await guardian.close_position(api, 'BTCUSDT', bias, qty, state, reason='HARD_SL_FAILED')
            release_execution(state, signal, 'INVALIDATED')
            return False

        if position.split_sl_enabled:
            logging.info(
                "🪶 [SPLIT SL] POC+VA edge: SL1 %.2f cắt tối đa 90%%; "
                "SL2 %.2f giữ tail 10%% | extra=%.2f",
                position.split_sl1, position.split_sl2,
                float(split_sl_policy.get('sl2_extra_distance', 0.0) or 0.0),
            )

        journal_mod.mark_actual_open(
            state, cycle_id, avg_price,
            strategy_entry_price=shadow_price,
        )
        if not mainnet_fixed and os.getenv(
            'SMC_MINIMAL_MAINNET_AUDIT', 'false'
        ).lower() not in ('1', 'true', 'yes', 'on'):
            journal_mod.activate_shadow(state, cycle_id, signal, qty, shadow_entry)

        logging.info(
            "✅ [EXECUTOR] Entry %s %.4f | Mainnet strategy %.2f "
            "(Soft SL %.2f, TP1 %.2f, TP2 %.2f) | venue fill %.2f, "
            "Hard SL %.2f (algoId=%s)",
            bias, qty, shadow_price, position.soft_sl, position.soft_tp1,
            position.soft_tp2, avg_price, position.hard_sl,
            position.hard_sl_algo_id,
        )
        _clear_retry_cycle(state, signal, setup)
        complete_execution(state, signal)
        return True
    except Exception:
        if order_confirmed:
            state.execution_unknown = True
            state.execution_unknown_since = time.time()
            state.system_ready = False
            state.trading_enabled = False
            state.last_readiness_reason = 'Entry đã gửi nhưng hậu xử lý chưa hoàn tất'
        else:
            release_execution(state, signal, 'INVALIDATED')
        raise
    finally:
        state.dang_xu_ly_dong_lenh = False
