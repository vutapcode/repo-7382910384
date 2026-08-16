"""Trade Guardian: local SL/TP/eject và đóng vị thế idempotent."""

import asyncio
import logging
import math
import time
import importlib.util
from pathlib import Path

from loi_he_thong import mainnet_safety, strategy_profile

try:
    from loi_he_thong.order_identity import client_order_id as forensic_order_id
except ModuleNotFoundError:
    _identity_spec = importlib.util.spec_from_file_location(
        'guardian_order_identity',
        Path(__file__).resolve().parents[2] / 'loi_he_thong' / 'order_identity.py',
    )
    _identity_mod = importlib.util.module_from_spec(_identity_spec)
    _identity_spec.loader.exec_module(_identity_mod)
    forensic_order_id = _identity_mod.client_order_id


CURRENT_DIR = Path(__file__).resolve().parent


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shark_mod = _load_module(
    'guardian_shark_context',
    CURRENT_DIR.parents[1] / '2_suy_luan_mapping' / 'tong_ket_chi_huy' / 'shark_context.py',
)
journal_mod = _load_module(
    'nhat_ky_giao_dich_guardian',
    CURRENT_DIR.parent / 'quan_ly_vi_the' / 'nhat_ky_giao_dich.py',
)
risk_mod = _load_module(
    'tinh_toan_rui_ro_guardian',
    CURRENT_DIR.parent / 'quan_ly_vi_the' / 'tinh_toan_rui_ro.py',
)
snapshot_mod = _load_module(
    'decision_snapshot_guardian',
    CURRENT_DIR.parents[1] / '2_suy_luan_mapping' / 'tong_ket_chi_huy'
    / 'decision_snapshot.py',
)
continuous_mod = _load_module(
    'continuous_v2_guardian',
    CURRENT_DIR.parents[1] / '2_suy_luan_mapping' / 'tong_ket_chi_huy'
    / 'cham_diem_continuous_v2.py',
)
dynamic_path_mod = _load_module(
    'dynamic_path_guardian',
    CURRENT_DIR.parents[1] / '2_suy_luan_mapping' / 'tong_ket_chi_huy'
    / 'dynamic_path_fee.py',
)
economic_mod = _load_module(
    'economic_guardian',
    CURRENT_DIR.parents[1] / '2_suy_luan_mapping' / 'tong_ket_chi_huy'
    / 'kinh_te_lenh.py',
)


EARLY_PROTECTION_REASONS = frozenset({
    'SHARK_ADVERSE_CONFIRMED',
    'TIME_STOP',
})
AUG13_GUARDIAN_VERSION = 'AUG13_CAUSAL_GUARDIAN_V1'
AUG13_GUARDIAN_SCORE_INTERVAL = 0.25
AUG13_GUARDIAN_BASE_CONFIRM_SECONDS = 2.0


def is_early_protection_reason(reason):
    return reason in EARLY_PROTECTION_REASONS


def should_extend_tp2(position, shark):
    return (
        not strategy_profile.aug13_early_hybrid_enabled()
        and
        position.tp1_done
        and shark.get('status') == 'SHARK_SUPPORTIVE'
        and shark.get('support_count', 0) >= 2
        and shark.get('adverse_count', 0) == 0
    )


def should_eject_for_shark(side, current, ema9, shark, confirmed_for):
    # [TRI-ORACLE NOTE] Tri-Oracle DIVERGENCE check duoc thuc hien tai vong_lap_bao_ve
    # khi co state object. Ham nay khong nhan state.
    price_damage = (
        ema9 > 0 and (
            (side == 'LONG' and current < ema9)
            or (side == 'SHORT' and current > ema9)
        )
    )
    adverse = set(shark.get('adverse', []))
    directional_confirmation = bool(adverse.intersection({'FLASH_FLOW', 'FOOTPRINT'}))
    return (
        shark.get('status') == 'SHARK_ADVERSE'
        and confirmed_for >= 1.0
        and (
            shark.get('adverse_count', 0) >= 3
            or (price_damage and directional_confirmation)
        )
    )


def _dedupe_shark_families(names):
    mapping = {
        'FLASH_FLOW': 'AGGTRADE_FLOW',
        'FOOTPRINT': 'AGGTRADE_FLOW',
        'ABSORPTION_REACTION': 'PRICE_FLOW_REACTION',
        'BOOK': 'DEPTH',
        'OI_FUNDING': 'MACRO',
        'FLOW': 'AGGTRADE_FLOW',
    }
    return sorted({mapping.get(str(name), str(name)) for name in names or ()})


def assess_aug13_causal_exit(
    side, current, ema9, shark, continuous_score, remaining_plan,
    structure_transition='', structure_break_streak=0, age_seconds=0.0,
    setup_terminal=None,
):
    """Pure causal exit contract; one noisy label can never close Mainnet."""
    side = str(side or '').upper()
    sign = 1.0 if side == 'LONG' else -1.0
    score = dict(continuous_score or {})
    sides = dict(score.get('sides') or {})
    held = dict(sides.get(side) or {})
    opposite_side = 'SHORT' if side == 'LONG' else 'LONG'
    opposing = dict(sides.get(opposite_side) or {})
    momentum = dict(score.get('momentum_breakdown') or {})
    horizons = dict(momentum.get('horizons') or {})

    def directional_progress(horizon):
        row = dict(horizons.get(str(horizon)) or {})
        return sign * float(row.get('price_progress_atr', 0.0) or 0.0)

    progress_15 = directional_progress(15)
    progress_60 = directional_progress(60)
    ema_damage = bool(
        float(ema9 or 0.0) > 0.0 and (
            (side == 'LONG' and float(current) < float(ema9))
            or (side == 'SHORT' and float(current) > float(ema9))
        )
    )
    progress_damage = bool(
        progress_15 <= -0.18
        or (progress_15 <= -0.08 and progress_60 <= 0.0)
    )
    price_damage = bool(ema_damage and progress_damage)

    adverse_families = _dedupe_shark_families(shark.get('adverse', ()))
    support_families = _dedupe_shark_families(shark.get('support', ()))
    independent_adverse = len(adverse_families)
    held_power = float(held.get('trade_power', 0.0) or 0.0)
    opposing_power = float(opposing.get('trade_power', 0.0) or 0.0)
    floor = float(score.get('activation_floor', 0.0) or 0.0)
    opposing_dominant = bool(
        opposing_power >= held_power + max(2.0, 0.10 * floor)
    )

    plan = dict(remaining_plan or {})
    edge_available = bool(plan.get('available'))
    remaining_edge = plan.get('realizable_edge_lcb')
    economics_lost = bool(
        edge_available and remaining_edge is not None
        and float(remaining_edge) <= 0.0
    )
    transition = str(structure_transition or '').upper()
    structural_reversal = bool(
        int(structure_break_streak or 0) >= 2
        and (
            side == 'LONG' and transition == 'TRANSITION_BEARISH'
            or side == 'SHORT' and transition == 'TRANSITION_BULLISH'
        )
    )
    structural_candidate = bool(
        structural_reversal and price_damage and independent_adverse >= 1
    )
    composite_candidate = bool(
        independent_adverse >= 2
        and price_damage
        and opposing_dominant
        and economics_lost
    )
    terminal = dict(setup_terminal or {})
    setup_invalidated = bool(terminal.get('state') == 'INVALIDATED')
    lifecycle_candidate = bool(
        setup_invalidated and price_damage
        and (independent_adverse >= 1 or opposing_dominant or economics_lost)
    )
    age_pressure = max(0.0, min(
        1.0, (float(age_seconds or 0.0) - 2700.0) / 4500.0
    ))
    confirmation_seconds = (
        0.5 if structural_candidate or lifecycle_candidate
        else max(1.0, AUG13_GUARDIAN_BASE_CONFIRM_SECONDS - age_pressure)
    )
    return {
        'version': AUG13_GUARDIAN_VERSION,
        'side': side,
        'candidate': bool(
            structural_candidate or composite_candidate or lifecycle_candidate
        ),
        'structural_candidate': structural_candidate,
        'composite_candidate': composite_candidate,
        'lifecycle_candidate': lifecycle_candidate,
        'confirmation_seconds': confirmation_seconds,
        'adverse_families': adverse_families,
        'support_families': support_families,
        'independent_adverse_count': independent_adverse,
        'ema_damage': ema_damage,
        'progress_damage': progress_damage,
        'price_damage': price_damage,
        'progress_15_atr': progress_15,
        'progress_60_atr': progress_60,
        'held_trade_power': held_power,
        'opposing_trade_power': opposing_power,
        'activation_floor': floor,
        'opposing_dominant': opposing_dominant,
        'remaining_plan_available': edge_available,
        'remaining_realizable_edge_lcb': remaining_edge,
        'economics_lost': economics_lost,
        'structure_transition': transition,
        'structure_break_streak': int(structure_break_streak or 0),
        'structural_reversal': structural_reversal,
        'setup_invalidated': setup_invalidated,
        'setup_invalidation_reason': terminal.get('reason'),
        'age_seconds': float(age_seconds or 0.0),
        'age_pressure': age_pressure,
    }


def _position_setup_terminal(state, position):
    registry = getattr(state, 'setup_terminal_by_identity', {}) or {}
    identity = (
        str(getattr(position, 'setup_id', '') or ''),
        int(getattr(position, 'setup_generation', 0) or 0),
    )
    terminal = dict(registry.get(identity) or {}) if isinstance(registry, dict) else {}
    if terminal and time.time() - float(terminal.get('ts', 0.0) or 0.0) > 300.0:
        return {}
    return terminal


def _aug13_guardian_snapshot(state, position, current, now):
    setup = {
        'setup_id': getattr(position, 'setup_id', ''),
        'generation': int(getattr(position, 'setup_generation', 0) or 0),
        'semantic_key': getattr(position, 'setup_semantic_key', ''),
        'opportunity_id': (
            getattr(position, 'opportunity_id', '')
            or getattr(position, 'setup_semantic_key', '')
        ),
        'mode': getattr(position, 'mode', ''),
        'bias': getattr(position, 'side', ''),
        'zone': float(getattr(position, 'setup_zone', 0.0) or 0.0),
        'kind': 'breakout' if 'BREAKOUT' in str(
            getattr(position, 'mode', '')
        ).upper() else 'zone',
        'entry_style': 'POSITION_GUARDIAN',
        'breakout_target': getattr(position, 'breakout_target', 0.0),
        'breakout_target2': getattr(position, 'breakout_target2', 0.0),
    }
    snapshot = snapshot_mod.capture(state, setup, now, time.monotonic())
    score = continuous_mod.score_continuous(
        snapshot, setup, {'mode': setup['mode']}, live=False
    )
    tick = float((getattr(state, 'exchange_filters', {}) or {}).get(
        'tick_size', 0.1
    ) or 0.1)
    signal = {
        'bias': position.side,
        'mode': position.mode,
        'continuous_score': score,
        'breakout_target': float(position.soft_tp1),
        'breakout_target2': float(position.soft_tp2),
    }
    remaining_plan = dynamic_path_mod.plan_exit(
        snapshot, signal, float(position.qty), float(current),
        float(getattr(position, 'strategy_hard_sl', position.hard_sl)),
        tick_size=tick,
        filters=dict(getattr(state, 'exchange_filters', {}) or {}),
        entry_fee_bps=0.0,
        exit_fee_bps=float(economic_mod.EXIT_FEE_BPS),
        entry_slippage_bps=0.0,
        exit_slippage_bps=0.0,
    )
    return score, remaining_plan


def _record_aug13_guardian_assessment(state, position, assessment, now, force=False):
    cycle = getattr(state, 'trade_cycles', {}).get(
        getattr(position, 'position_cycle_id', '')
    )
    if cycle is None:
        return
    block = cycle.setdefault('guardian_breakdown', {
        'version': AUG13_GUARDIAN_VERSION, 'last': None, 'history': [],
    })
    previous = dict(block.get('last') or {})
    compact = dict(assessment)
    compact['ts'] = float(now)
    block['last'] = compact
    signature = (
        compact.get('candidate'), compact.get('structural_candidate'),
        compact.get('price_damage'), compact.get('opposing_dominant'),
        compact.get('economics_lost'),
        tuple(compact.get('adverse_families', ())),
    )
    previous_signature = (
        previous.get('candidate'), previous.get('structural_candidate'),
        previous.get('price_damage'), previous.get('opposing_dominant'),
        previous.get('economics_lost'),
        tuple(previous.get('adverse_families', ())),
    )
    last_emit = float(block.get('last_emit_at', 0.0) or 0.0)
    if force or signature != previous_signature or now - last_emit >= 15.0:
        history = list(block.get('history') or ())
        history.append(compact)
        block['history'] = history[-64:]
        block['last_emit_at'] = float(now)
        journal_mod.record_decision_stage(
            state, 'AUG13_GUARDIAN_ASSESSMENT', compact,
            cycle_id=getattr(position, 'position_cycle_id', None),
        )


def log_if_exception(task):
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logging.critical("🔥 [GUARDIAN] Task đóng lệnh lỗi: %s", exc)


def _floor_step(value, step):
    return round(math.floor((value + 1e-12) / step) * step, 12)


def _close_kwargs(state, side):
    if getattr(state, 'account_hedge_mode', True):
        return {'positionSide': side}
    return {'reduceOnly': 'true'}


def _execution_prices(state):
    """Bid/ask Testnet để audit và tham chiếu lúc gửi lệnh."""
    return (
        float(getattr(state, 'execution_best_bid', 0.0) or 0.0),
        float(getattr(state, 'execution_best_ask', 0.0) or 0.0),
    )


def _strategy_prices(state):
    """Bid/ask Mainnet là nguồn duy nhất cho quyết định Guardian."""
    return (
        float(getattr(state, 'best_bid', 0.0) or 0.0),
        float(getattr(state, 'best_ask', 0.0) or 0.0),
    )


def _close_price_context(state, side):
    strategy_bid, strategy_ask = _strategy_prices(state)
    execution_bid, execution_ask = _execution_prices(state)
    return {
        'strategy_trigger_price': strategy_bid if side == 'LONG' else strategy_ask,
        'execution_reference_price': execution_bid if side == 'LONG' else execution_ask,
    }


def _filled_result(result, requested_qty, step):
    if not isinstance(result, dict) or result.get('status') != 'FILLED':
        return False
    executed = float(result.get('executedQty', 0.0) or 0.0)
    return executed >= max(0.0, float(requested_qty) - step / 2)


def _confirmed_close_payload(pending, result, remaining, source):
    payload = dict(result) if isinstance(result, dict) else {}
    payload.setdefault('clientOrderId', pending['client_order_id'])
    if pending.get('order_id') is not None:
        payload.setdefault('orderId', pending['order_id'])
    payload['_confirmed_remaining_qty'] = max(0.0, float(remaining))
    payload['_confirmation_source'] = source
    payload['_pending_tag'] = pending.get('tag')
    payload['_requested_qty'] = pending.get('requested_qty')
    payload['_pending_reason'] = pending.get('reason')
    payload['_strategy_trigger_price'] = pending.get('strategy_trigger_price', 0.0)
    payload['_execution_reference_price'] = pending.get('execution_reference_price', 0.0)
    return payload


async def _recheck_pending_close(api, symbol, state, pending):
    """Chỉ query cùng clientOrderId; không bao giờ POST ID thứ hai."""
    now = time.time()
    if now < float(pending.get('next_check_at', 0.0)):
        return {'status': 'CLOSE_PENDING', 'clientOrderId': pending['client_order_id']}, 202

    pending['check_count'] = int(pending.get('check_count', 0)) + 1
    pending['next_check_at'] = now + min(1.0, 0.15 * pending['check_count'])
    step = float(getattr(state, 'exchange_filters', {}).get('step_size', 0.001))
    requested = float(pending['requested_qty'])
    target_remaining = max(0.0, float(pending['before_qty']) - requested)

    queried, query_status = await api.query_order(symbol, pending['client_order_id'])
    if query_status == 200:
        pending['order_result'] = queried
        pending['order_id'] = queried.get('orderId', pending.get('order_id'))
        if _filled_result(queried, requested, step):
            state.pending_close = None
            return _confirmed_close_payload(
                pending, queried, target_remaining, 'ORDER_QUERY_FILLED'
            ), 200

    remaining = await _exchange_remaining_qty(api, state, symbol, pending['side'])
    if remaining is not None and remaining <= target_remaining + step / 2:
        state.pending_close = None
        result = pending.get('order_result') or queried
        return _confirmed_close_payload(
            pending, result, remaining, 'POSITION_QTY_CHANGED'
        ), 200

    order_state = queried.get('status') if isinstance(queried, dict) else None
    if query_status == 200 and order_state in ('REJECTED', 'EXPIRED', 'CANCELED'):
        state.pending_close = None
        return queried, 400

    if now - float(pending.get('last_warning_at', 0.0)) >= 5.0:
        pending['last_warning_at'] = now
        logging.warning(
            "⏳ [GUARDIAN] Close đang chờ xác nhận; giữ clientOrderId=%s, không POST lại.",
            pending['client_order_id'],
        )
    return {'status': 'CLOSE_PENDING', 'clientOrderId': pending['client_order_id']}, 202


async def _submit_close_order(api, symbol, side, qty, state, tag, reason=None):
    pending = getattr(state, 'pending_close', None)
    if pending is not None:
        # Một vị thế chỉ có một close đang bay. Tag khác cũng phải chờ ID hiện tại xong.
        return await _recheck_pending_close(api, symbol, state, pending)

    now = time.time()
    position = getattr(state, 'vi_the_hien_tai', None)
    client_order_id = forensic_order_id(
        state, tag,
        opportunity_id=getattr(position, 'setup_semantic_key', None),
        setup_id=getattr(position, 'setup_id', None),
        generation=getattr(position, 'setup_generation', 0),
        nonce=int(now * 1000),
    )
    price_context = _close_price_context(state, side)
    pending = {
        'client_order_id': client_order_id,
        'side': side,
        'tag': tag,
        'reason': reason or tag.upper(),
        'before_qty': float(state.vi_the_hien_tai.qty),
        'requested_qty': float(qty),
        'position_cycle_id': getattr(state.vi_the_hien_tai, 'position_cycle_id', ''),
        # trigger_price giữ tương thích ROM cũ nhưng nay mang nghĩa Mainnet.
        'trigger_price': price_context['strategy_trigger_price'],
        'strategy_trigger_price': price_context['strategy_trigger_price'],
        'execution_reference_price': price_context['execution_reference_price'],
        'created_at': now,
        'next_check_at': now + 0.15,
        'check_count': 0,
        'last_warning_at': 0.0,
        'order_result': None,
        'order_id': None,
    }
    state.pending_close = pending
    result, status = await api.new_order(
        symbol,
        'SELL' if side == 'LONG' else 'BUY',
        'MARKET',
        qty,
        newOrderRespType='RESULT',
        newClientOrderId=client_order_id,
        **_close_kwargs(state, side),
    )
    pending['order_result'] = result if isinstance(result, dict) else {}
    pending['order_id'] = result.get('orderId') if isinstance(result, dict) else None
    step = float(getattr(state, 'exchange_filters', {}).get('step_size', 0.001))
    target_remaining = max(0.0, pending['before_qty'] - pending['requested_qty'])

    # RESULT/FILLED là xác nhận fill có thẩm quyền; position endpoint có thể trễ vài giây.
    if status == 200 and _filled_result(result, qty, step):
        state.pending_close = None
        return _confirmed_close_payload(
            pending, result, target_remaining, 'ORDER_POST_FILLED'
        ), 200
    if status in (200, 599):
        return {'status': 'CLOSE_PENDING', 'clientOrderId': client_order_id}, 202

    # -2022 có thể xảy ra khi Hard SL vừa đóng trước POST; xác minh position một lần.
    remaining = await _exchange_remaining_qty(api, state, symbol, side)
    if remaining is not None and remaining <= target_remaining + step / 2:
        state.pending_close = None
        return _confirmed_close_payload(
            pending, result, remaining, 'POSITION_AFTER_REJECT'
        ), 200
    state.pending_close = None
    return result, status


async def _exchange_remaining_qty(api, state, symbol, side):
    positions, status = await api.get_positions(symbol)
    if status != 200:
        return None
    wanted = side if getattr(state, 'account_hedge_mode', True) else 'BOTH'
    for item in positions:
        if item.get('positionSide') == wanted:
            return abs(float(item.get('positionAmt', 0.0)))
    return 0.0


def clear_local_position(state):
    position = state.vi_the_hien_tai
    semantic_key = getattr(position, 'setup_semantic_key', '') or (
        str(getattr(position, 'setup_id', '')).rsplit(':a', 1)[0]
        if ':a' in str(getattr(position, 'setup_id', '')) else ''
    )
    if semantic_key:
        state.setup_cooldowns[semantic_key] = time.monotonic() + 60.0
        state.rearm_blocks[semantic_key] = {
            'zone': float(getattr(position, 'setup_zone', 0.0) or 0.0),
            'blocked_at_mono': time.monotonic(),
        }
    state.co_lenh_mo = False
    state.pending_close = None
    position.active = False
    position.side = ''
    position.qty = 0.0
    position.initial_qty = 0.0
    position.protection_closed_qty = 0.0
    position.protection_reasons_done = []
    position.entry_price = 0.0
    position.strategy_entry_price = 0.0
    position.execution_entry_price = 0.0
    position.hard_sl = 0.0
    position.strategy_hard_sl = 0.0
    position.soft_sl = 0.0
    position.soft_tp1 = 0.0
    position.soft_tp2 = 0.0
    position.tp1_allocation = 0.50
    position.tp1_checkpoint_monetizable = False
    position.tp1_checkpoint_lock_net_bps = 0.0
    position.runner_policy = 'LEGACY_TP2'
    position.sl_order_id = None
    position.hard_sl_algo_id = None
    position.hard_sl_client_algo_id = None
    position.split_sl_enabled = False
    position.split_sl1_done = False
    position.split_sl1_fraction = 0.90
    position.split_sl1 = 0.0
    position.split_sl2 = 0.0
    position.standard_hard_sl = 0.0
    position.opened_at = 0.0
    position.tp1_done = False
    position.trailing_active = False
    position.add_on_done = False
    position.add_on_attempted = False
    position.mode = ''
    position.setup_id = ''
    position.setup_semantic_key = ''
    position.setup_zone = 0.0
    position.venue_price_offset = 0.0
    position.setup_generation = 0
    position.position_cycle_id = ''
    position.entry_order_id = None
    position.entry_client_order_id = None
    position.tp2_extended = False
    position.shark_adverse_since = 0.0
    position.shark_support_since = 0.0


def split_sl_soft_action(position):
    if not getattr(position, 'split_sl_enabled', False):
        return 'FULL_SOFT_SL'
    return (
        'TAIL_TO_SL2'
        if getattr(position, 'split_sl1_done', False) else 'EXECUTE_SL1'
    )


def mark_unmonetized_tp1(position):
    """Record a waypoint without granting it stop/trailing authority."""
    position.tp1_done = True
    return False


async def close_partial(api, symbol, side, qty, state, reason='TP1'):
    if qty <= 0:
        return False
    protective = is_early_protection_reason(reason)
    before_qty = float(state.vi_the_hien_tai.qty)
    tag = 'protect' if protective else 'sl1' if reason == 'SL1_90' else 'tp1'
    result, status = await _submit_close_order(
        api, symbol, side, qty, state, tag, reason=reason,
    )
    if status == 202:
        return False
    if status != 200:
        logging.error("❌ [GUARDIAN] Partial close %s thất bại: %s", reason, result)
        return False
    cycle_id = getattr(state.vi_the_hien_tai, 'position_cycle_id', '')
    strategy_trigger = float(result.get('_strategy_trigger_price', 0.0) or 0.0)
    execution_reference = float(result.get('_execution_reference_price', 0.0) or 0.0)
    if cycle_id:
        role = 'PROTECT' if protective else 'SL1' if reason == 'SL1_90' else 'TP1'
        journal_mod.record_actual_order(
            state, cycle_id, role,
            result, qty, execution_reference,
            reason=reason,
            strategy_reference_price=strategy_trigger,
            execution_reference_price=execution_reference,
        )
    remaining = float(
        result.get('_confirmed_remaining_qty', max(0.0, state.vi_the_hien_tai.qty - qty))
    )
    state.vi_the_hien_tai.qty = remaining
    if reason == 'SL1_90':
        state.vi_the_hien_tai.split_sl1_done = True
    if protective:
        actually_closed = max(0.0, before_qty - remaining)
        maximum = float(state.vi_the_hien_tai.initial_qty) * 0.5
        state.vi_the_hien_tai.protection_closed_qty = min(
            maximum,
            float(getattr(state.vi_the_hien_tai, 'protection_closed_qty', 0.0) or 0.0)
            + actually_closed,
        )
        reasons = state.vi_the_hien_tai.protection_reasons_done
        if reason not in reasons:
            reasons.append(reason)
    logging.info("✅ [GUARDIAN] Partial close %s %.4f; còn %.4f", reason, qty, remaining)
    return True


async def close_early_protection(api, symbol, side, state, reason):
    """Reduce risk once, while preserving at least half of the original entry for SL."""
    if not is_early_protection_reason(reason):
        raise ValueError(f'Không phải early-protection reason: {reason}')
    position = state.vi_the_hien_tai
    reasons = getattr(position, 'protection_reasons_done', [])
    if reason in reasons:
        return False
    if not bool(getattr(api, 'testnet', True)):
        # Mainnet quantity is exactly one exchange step (0.001 BTC), so a
        # partial protection order cannot be represented. Preserve the same
        # confirmed Guardian trigger but close the complete position.
        reasons.append(reason)
        return await close_position(
            api, symbol, side, position.qty, state, reason
        )
    if getattr(position, 'split_sl_enabled', False):
        reasons.append(reason)
        logging.info(
            "🪶 [SPLIT SL] Bỏ qua %s để SL1 giữ quyền cắt đúng 90%% và SL2 giữ 10%%.",
            reason,
        )
        return False
    step = float(state.exchange_filters.get('step_size', 0.001))
    qty = risk_mod.calculate_early_protection_qty(
        position.qty,
        position.initial_qty,
        getattr(position, 'protection_closed_qty', 0.0),
        position.qty,
        step,
    )
    current = float(
        state.best_bid if side == 'LONG' else state.best_ask
    )
    min_qty = float(state.exchange_filters.get('min_qty', step))
    min_notional = float(state.exchange_filters.get('min_notional', 0.0))
    if qty < max(step, min_qty) or (min_notional and qty * current < min_notional):
        reasons.append(reason)
        logging.info(
            "🧱 [GUARDIAN] %s không cắt thêm: quota 50%%/min-notional; giữ %.4f cho SL.",
            reason, position.qty,
        )
        return False
    return await close_partial(api, symbol, side, qty, state, reason)


async def close_position(api, symbol, side, qty, state, reason='GUARDIAN'):
    state.dang_xu_ly_dong_lenh = True
    try:
        position = state.vi_the_hien_tai
        cycle_id = getattr(position, 'position_cycle_id', '')
        result, status = await _submit_close_order(
            api, symbol, side, qty, state, 'close', reason=reason
        )
        if status == 202:
            return None
        if status != 200:
            logging.error("❌ [GUARDIAN] Close %s thất bại: %s", reason, result)
            return False

        strategy_trigger = float(result.get('_strategy_trigger_price', 0.0) or 0.0)
        execution_reference = float(result.get('_execution_reference_price', 0.0) or 0.0)

        remaining = float(result.get('_confirmed_remaining_qty', 0.0))
        step = float(getattr(state, 'exchange_filters', {}).get('step_size', 0.001))
        if remaining >= step / 2:
            state.vi_the_hien_tai.qty = remaining
            logging.info("ℹ️ [GUARDIAN] Close một phần; còn %.8f", remaining)
            return False

        if cycle_id:
            journal_mod.record_actual_order(
                state, cycle_id, 'CLOSE', result, qty, execution_reference,
                reason=reason,
                strategy_reference_price=strategy_trigger,
                execution_reference_price=execution_reference,
            )
            journal_mod.mark_actual_closed(
                state, cycle_id, reason, strategy_trigger, result=result,
                execution_reference_price=execution_reference,
            )
        try:
            mainnet_safety.register_confirmed_mainnet_close(
                state, position, result, execution_reference
            )
        except Exception:
            # A ledger write cannot interfere with an already confirmed close;
            # journal reconciliation will retry the outcome later.
            logging.exception(
                "⚠️ [MAINNET SAFETY] Chưa ghi được loss-streak sau close"
            )

        algo_id = state.vi_the_hien_tai.hard_sl_algo_id
        if algo_id is not None:
            cancel_result, cancel_status = await api.cancel_algo_order(algo_id)
            cancel_code = cancel_result.get('code') if isinstance(cancel_result, dict) else None
            if cancel_status != 200 and cancel_code != -2011:
                # Có thể SL đã trigger/cancel; reconciliation sẽ kiểm tra lại.
                logging.warning("⚠️ [GUARDIAN] Không cancel được Hard SL %s: %s", algo_id, cancel_result)
        clear_local_position(state)
        logging.info("✅ [GUARDIAN] Đóng toàn bộ vị thế (%s)", reason)
        return True
    finally:
        state.dang_xu_ly_dong_lenh = False


async def vong_lap_bao_ve(state, binance_api, symbol='BTCUSDT'):
    if (
        getattr(state, 'execution_venue', '') == 'BINANCE_FUTURES_MAINNET'
        and strategy_profile.aug13_early_hybrid_enabled()
    ):
        logging.info(
            "🛡️ [TRADE GUARDIAN] AUG13 causal full-exit; một nhãn Shark "
            "không đủ đóng 0.001 BTC."
        )
    elif getattr(state, 'execution_venue', '') == 'BINANCE_FUTURES_MAINNET':
        logging.info(
            "🛡️ [TRADE GUARDIAN] MAINNET full-position exit; "
            "không split SL/TP hoặc partial protection."
        )
    else:
        logging.info(
            "🛡️ [TRADE GUARDIAN] Bảo vệ sớm cắt cộng dồn tối đa 50%%; "
            "phần còn lại cho SL."
        )
    while True:
        try:
            if not state.co_lenh_mo or not state.vi_the_hien_tai.active:
                await asyncio.sleep(0.1)
                continue
            if state.dang_xu_ly_dong_lenh:
                await asyncio.sleep(0.01)
                continue

            position = state.vi_the_hien_tai
            side = position.side
            
            # [TIER S - TRI-ORACLE] Eject khan cap khi Coinbase phan ky voi Futures
            # Neu dang giu Long ma Coinbase dang xa manh -> nhay du ngay
            if getattr(state, 'tri_oracle_signal', 'NEUTRAL') == 'DIVERGENCE':
                if side == 'LONG' and getattr(state, 'coinbase_cvd_1m', 0.0) < -0.5:
                    await close_position(binance_api, symbol, side, position.qty, state, 'TRI_ORACLE_EJECT')
                    continue
                if side == 'SHORT' and getattr(state, 'coinbase_cvd_1m', 0.0) > 0.5:
                    await close_position(binance_api, symbol, side, position.qty, state, 'TRI_ORACLE_EJECT')
                    continue
                    
            pending = getattr(state, 'pending_close', None)
            if pending:
                # Chính loại lệnh đã POST phải tự reap confirmation; không để TP1/CLOSE
                # đổi vai rồi ghi journal sai khi market chuyển trạng thái trong lúc chờ.
                if pending.get('tag') in ('tp1', 'protect', 'sl1'):
                    await close_partial(
                        binance_api, symbol, side,
                        float(pending.get('requested_qty', 0.0)), state,
                        pending.get('reason', 'TP1'),
                    )
                else:
                    await close_position(
                        binance_api, symbol, side,
                        float(pending.get('requested_qty', position.qty)), state,
                        pending.get('reason', 'GUARDIAN'),
                    )
                await asyncio.sleep(0.01)
                continue
            context = getattr(state, 'shark_context', {}) or {}
            loop_now = time.time()
            shark = (
                context if (
                    context.get('side') == side
                    and loop_now - float(context.get('ts', 0.0)) <= 0.3
                )
                else shark_mod.evaluate(state, side, loop_now)
            )
            strategy_bid, strategy_ask = _strategy_prices(state)
            execution_bid, execution_ask = _execution_prices(state)
            if strategy_bid <= 0.0 or strategy_ask <= strategy_bid:
                state.system_ready = False
                state.trading_enabled = False
                state.last_readiness_reason = 'Mainnet strategy price không hợp lệ'
                await asyncio.sleep(0.1)
                continue
            if execution_bid <= 0.0 or execution_ask <= execution_bid:
                state.system_ready = False
                state.trading_enabled = False
                state.last_readiness_reason = 'Testnet execution price không hợp lệ'
                await asyncio.sleep(0.1)
                continue
            current = strategy_bid if side == 'LONG' else strategy_ask
            spread = strategy_ask - strategy_bid
            atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
            spread_too_high = atr > 0 and spread > 0.5 * atr

            if side == 'LONG':
                sl_hit = current <= position.soft_sl
                tp1_hit = current >= position.soft_tp1
                tp2_hit = current >= position.soft_tp2
            else:
                sl_hit = current >= position.soft_sl
                tp1_hit = current <= position.soft_tp1
                tp2_hit = current <= position.soft_tp2

            split_sl_enabled = bool(
                getattr(position, 'split_sl_enabled', False)
            )
            if sl_hit and not spread_too_high:
                split_action = split_sl_soft_action(position)
                if split_action == 'EXECUTE_SL1':
                    step = float(state.exchange_filters.get('step_size', 0.001))
                    sl1_qty = risk_mod.calculate_split_sl1_close_qty(
                        position.qty, position.initial_qty, step
                    )
                    min_qty = float(state.exchange_filters.get('min_qty', step))
                    min_notional = float(
                        state.exchange_filters.get('min_notional', 0.0)
                    )
                    if (
                        sl1_qty >= max(step, min_qty)
                        and (not min_notional or sl1_qty * current >= min_notional)
                    ):
                        await close_partial(
                            binance_api, symbol, side, sl1_qty, state, 'SL1_90'
                        )
                    else:
                        position.split_sl1_done = True
                        logging.warning(
                            "🪶 [SPLIT SL] SL1 không đủ filter; không đóng quá "
                            "90%%, giữ %.4f cho SL2.",
                            position.qty,
                        )
                    # A close POST/recheck must always yield to reconcile,
                    # journal and market-data tasks.
                    await asyncio.sleep(0.01)
                    continue
                elif split_action == 'TAIL_TO_SL2':
                    # SL1 already reserved the 10% tail.  Do not spin on the
                    # still-true soft-SL condition; SL2/TP/normal management
                    # retains authority over the remainder.
                    pass
                else:
                    await close_position(
                        binance_api, symbol, side, position.qty, state, 'SOFT_SL'
                    )
                    continue
            if tp2_hit and should_extend_tp2(position, shark):
                if not position.tp2_extended:
                    position.tp2_extended = True
                    logging.info(
                        "🐋 [GUARDIAN] TP2 thành checkpoint; tiếp tục gồng vì cá mập hỗ trợ: %s",
                        shark.get('support'),
                    )
            elif tp2_hit:
                await close_position(binance_api, symbol, side, position.qty, state, 'TP2')
                continue
            if tp1_hit and not position.tp1_done:
                step = float(state.exchange_filters.get('step_size', 0.001))
                allocation = max(
                    0.0, min(0.70, float(
                        getattr(position, 'tp1_allocation', 0.50) or 0.0
                    ))
                )
                tp1_size = risk_mod.calculate_tp1_close_qty(
                    position.qty, position.initial_qty, allocation, current,
                    state.exchange_filters,
                )
                partial_qty = tp1_size['quantity']
                if not tp1_size['executable']:
                    # Nothing was monetized, therefore this near target cannot
                    # activate trailing or move the stop to entry.  It is only
                    # an informational waypoint on the way to the runner.
                    mark_unmonetized_tp1(position)
                    logging.info(
                        "🧭 [TP1] Checkpoint không thực thi; giữ nguyên SL và "
                        "quản lý runner %.2f.", position.soft_tp2,
                    )
                    continue
                if await close_partial(
                    binance_api, symbol, side, partial_qty, state, 'TP1'
                ):
                    position.tp1_done = True
                    position.trailing_active = True
                    position.soft_sl = position.entry_price
                continue

            now = loop_now
            status = shark.get('status')
            aug13_mainnet = bool(
                getattr(state, 'execution_venue', '')
                == 'BINANCE_FUTURES_MAINNET'
                and strategy_profile.aug13_early_hybrid_enabled()
            )
            if aug13_mainnet:
                last_assessment = float(getattr(
                    position, 'aug13_guardian_last_assessment_at', 0.0
                ) or 0.0)
                if now - last_assessment >= AUG13_GUARDIAN_SCORE_INTERVAL:
                    position.aug13_guardian_last_assessment_at = now
                    try:
                        score, remaining_plan = _aug13_guardian_snapshot(
                            state, position, current, now
                        )
                        opened_at = position.opened_at or state.last_signal_time
                        assessment = assess_aug13_causal_exit(
                            side, current,
                            float(getattr(state, 'ema9_m1', 0.0) or 0.0),
                            shark, score, remaining_plan,
                            getattr(state, 'structure_transition', ''),
                            getattr(state, 'structure_break_streak', 0),
                            max(0.0, now - float(opened_at or now)),
                            setup_terminal=_position_setup_terminal(
                                state, position
                            ),
                        )
                        if assessment['candidate']:
                            since = float(getattr(
                                position, 'aug13_exit_candidate_since', 0.0
                            ) or 0.0)
                            if since <= 0.0:
                                since = now
                                position.aug13_exit_candidate_since = now
                            confirmed_for = max(0.0, now - since)
                        else:
                            position.aug13_exit_candidate_since = 0.0
                            confirmed_for = 0.0
                        assessment['candidate_confirmed_for'] = confirmed_for
                        assessment['exit_now'] = bool(
                            assessment['candidate']
                            and confirmed_for
                            >= float(assessment['confirmation_seconds'])
                        )
                        _record_aug13_guardian_assessment(
                            state, position, assessment, now,
                            force=assessment['exit_now'],
                        )
                        if assessment['exit_now']:
                            await close_position(
                                binance_api, symbol, side, position.qty, state,
                                'AUG13_CAUSAL_REVERSAL',
                            )
                            continue
                    except Exception as exc:
                        # Scorer/path uncertainty must fail-safe to the
                        # exchange Hard SL, never synthesize an early exit.
                        position.aug13_exit_candidate_since = 0.0
                        last_error_log = float(getattr(
                            position, 'aug13_guardian_last_error_log_at', 0.0
                        ) or 0.0)
                        if now - last_error_log >= 5.0:
                            position.aug13_guardian_last_error_log_at = now
                            logging.exception(
                                "⚠️ [AUG13 GUARDIAN] Không đánh giá được; giữ "
                                "Hard SL/TP làm authority: %s", exc,
                            )
            elif status == 'SHARK_ADVERSE':
                position.shark_support_since = 0.0
                position.shark_adverse_since = position.shark_adverse_since or now
                ema9 = float(getattr(state, 'ema9_m1', 0.0) or 0.0)
                confirmed_for = now - position.shark_adverse_since
                if should_eject_for_shark(side, current, ema9, shark, confirmed_for):
                    await close_early_protection(
                        binance_api, symbol, side, state,
                        'SHARK_ADVERSE_CONFIRMED',
                    )
                    continue
            elif status == 'SHARK_SUPPORTIVE':
                position.shark_adverse_since = 0.0
                position.shark_support_since = position.shark_support_since or now
            else:
                position.shark_adverse_since = 0.0
                position.shark_support_since = 0.0

            opened_at = position.opened_at or state.last_signal_time
            guardian_policy = dict(getattr(position, 'guardian_policy', {}) or {})
            time_budget = (
                float(guardian_policy.get('time_budget_seconds', 4500.0) or 4500.0)
                if guardian_policy.get('mode') == 'BOUNDED_LIVE' else 4500.0
            )
            if (
                not aug13_mainnet
                and opened_at > 0 and now - opened_at > time_budget
            ):
                await close_early_protection(
                    binance_api, symbol, side, state, 'TIME_STOP'
                )
                continue
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("❌ [GUARDIAN] Lỗi vòng bảo vệ: %s", exc)
            await asyncio.sleep(1)
