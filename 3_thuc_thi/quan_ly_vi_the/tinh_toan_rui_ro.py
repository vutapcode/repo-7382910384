"""Pure risk calculations; chỉ đọc dữ liệu đã có trong RAM."""

import math
import os
import time


MIN_INITIAL_SOFT_SL_DISTANCE = 41.0
MAX_EARLY_PROTECTION_FRACTION = 0.50
SPLIT_SL1_CLOSE_FRACTION = 0.90
SPLIT_SL2_MIN_EXTRA_DISTANCE = 41.0
SPLIT_SL2_EXTRA_ATR = 2.0


def adaptive_guardian_policy(
    entry_price, structural_sl, target_price, atr, spread, tick_size,
    noise_scale=1.0, price_progress=0.0,
):
    """Continuous stop/time proposal; side enters only through geometry."""
    entry = float(entry_price or 0.0)
    atr = max(float(atr or 0.0), float(tick_size or 0.0), 1e-9)
    spread = max(0.0, float(spread or 0.0))
    noise = max(0.50, min(2.0, float(noise_scale or 1.0)))
    structural_distance = abs(entry - float(structural_sl or entry))
    stop_distance = max(
        structural_distance, 0.55 * atr * noise,
        4.0 * spread, 2.0 * float(tick_size or 0.0),
    )
    stop_distance = max(0.25 * atr, min(stop_distance, 4.0 * atr))
    # slippage_buffer được tính ngoài tại nơi có state; xem calculate_levels()
    path_atr = abs(float(target_price or entry) - entry) / atr
    progress = max(-1.0, min(1.0, float(price_progress or 0.0)))
    time_budget = 900.0 + 900.0 * min(path_atr, 4.0) * noise
    time_budget *= 1.0 - 0.30 * max(0.0, progress) + 0.20 * max(0.0, -progress)
    time_budget = max(900.0, min(7200.0, time_budget))
    return {
        'version': 'ADAPTIVE_GUARDIAN_V21',
        'stop_distance': stop_distance,
        'time_budget_seconds': time_budget,
        'structural_distance': structural_distance,
        'atr': atr, 'spread': spread, 'noise_scale': noise,
        'price_progress': progress,
        'mode': str(os.getenv('SMC_ADAPTIVE_GUARDIAN_MODE', 'SHADOW')).upper(),
    }


def floor_to_step(value, step):
    if step <= 0:
        return value
    precision = max(0, min(12, int(round(-math.log10(step))) + 2))
    return round(math.floor((value + 1e-12) / step) * step, precision)


def ceil_to_step(value, step):
    if step <= 0:
        return value
    precision = max(0, min(12, int(round(-math.log10(step))) + 2))
    return round(math.ceil((value - 1e-12) / step) * step, precision)


def round_to_tick(price, tick):
    if tick <= 0:
        return price
    precision = max(0, min(12, int(round(-math.log10(tick))) + 2))
    return round(round(price / tick) * tick, precision)


def build_value_area_split_sl_policy(
    side, entry_price, poc, vah, val, poc_modifier, atr, tick_size, levels,
):
    """90% dừng ở SL chuẩn, 10% tail dùng SL2 xa khi POC hút về value."""
    entry = float(entry_price or 0.0)
    poc = float(poc or 0.0)
    vah = float(vah or 0.0)
    val = float(val or 0.0)
    atr = float(atr or 0.0)
    tick = max(float(tick_size or 0.0), 1e-12)
    poc_supports = float(poc_modifier or 0.0) > 0.0
    crossed_value_edge = bool(
        (side == 'LONG' and val > 0.0 and entry <= val + tick and poc > entry)
        or (side == 'SHORT' and vah > 0.0 and entry >= vah - tick and poc < entry)
    )
    enabled = bool(
        side in ('LONG', 'SHORT') and entry > 0.0 and atr > 0.0
        and poc_supports and crossed_value_edge
    )
    standard_sl1 = float((levels or {}).get('soft_sl', 0.0) or 0.0)
    standard_hard_sl = float((levels or {}).get('hard_sl', 0.0) or 0.0)
    extra = max(SPLIT_SL2_MIN_EXTRA_DISTANCE, SPLIT_SL2_EXTRA_ATR * atr)
    sl2 = standard_hard_sl
    if enabled:
        sl2 = round_to_tick(
            standard_hard_sl - extra if side == 'LONG'
            else standard_hard_sl + extra,
            tick,
        )
    return {
        'enabled': enabled,
        'version': 'VALUE_AREA_POC_SPLIT_SL_V1',
        'sl1_close_fraction': SPLIT_SL1_CLOSE_FRACTION,
        'tail_fraction': 1.0 - SPLIT_SL1_CLOSE_FRACTION,
        'standard_sl1': standard_sl1,
        'standard_hard_sl': standard_hard_sl,
        'sl2': sl2,
        'sl2_extra_distance': extra if enabled else 0.0,
        'poc_modifier': float(poc_modifier or 0.0),
        'crossed_value_edge': crossed_value_edge,
        'poc_supports': poc_supports,
    }


def calculate_split_sl1_close_qty(current_qty, initial_qty, step_size):
    """Đóng tới mức còn đúng 10% entry gốc; không bao giờ vượt 90%."""
    step = max(float(step_size or 0.0), 1e-12)
    current = max(0.0, float(current_qty or 0.0))
    initial = max(0.0, float(initial_qty or 0.0))
    tail = ceil_to_step(initial * (1.0 - SPLIT_SL1_CLOSE_FRACTION), step)
    return floor_to_step(max(0.0, current - tail), step)


def validate_level_geometry(levels, entry_price, side, tick_size, atr=0.0):
    """Fail-closed khi SL/TP nằm sai phía so với giá vào cùng venue."""
    entry = float(entry_price or 0.0)
    tick = max(float(tick_size or 0.0), 1e-12)
    atr = max(float(atr or 0.0), 0.0)
    gap = max(tick, 0.05 * atr)
    hard_sl = float((levels or {}).get('hard_sl', 0.0) or 0.0)
    soft_sl = float((levels or {}).get('soft_sl', 0.0) or 0.0)
    tp1 = float((levels or {}).get('soft_tp1', 0.0) or 0.0)
    tp2 = float((levels or {}).get('soft_tp2', 0.0) or 0.0)
    if min(entry, hard_sl, soft_sl, tp1, tp2) <= 0.0:
        return False, 'LEVEL_MISSING_OR_NON_POSITIVE'
    if side == 'LONG':
        if not hard_sl < soft_sl <= entry - gap:
            return False, 'LONG_SL_NOT_BELOW_ENTRY'
        if not entry + tick <= tp1 < tp2:
            return False, 'LONG_TP_NOT_ABOVE_ENTRY'
    elif side == 'SHORT':
        if not hard_sl > soft_sl >= entry + gap:
            return False, 'SHORT_SL_NOT_ABOVE_ENTRY'
        if not entry - tick >= tp1 > tp2:
            return False, 'SHORT_TP_NOT_BELOW_ENTRY'
    else:
        return False, 'INVALID_SIDE'
    return True, 'PASS'


def translate_levels(levels, reference_entry, execution_entry, tick_size):
    """Giữ nguyên khoảng cách chiến lược nhưng neo sang giá của venue execution."""
    offset = float(execution_entry) - float(reference_entry)
    return {
        name: round_to_tick(float(levels[name]) + offset, tick_size)
        for name in ('hard_sl', 'soft_sl', 'soft_tp1', 'soft_tp2')
    }


def quantity_feasibility(equity_usdt, target_notional_pct, current_price, filters):
    """Return exact venue feasibility without ever increasing allocation."""
    equity = float(equity_usdt or 0.0)
    target_pct = max(0.0, float(target_notional_pct or 0.0))
    price = float(current_price or 0.0)
    if equity <= 0.0 or price <= 0.0:
        return {
            'executable': False, 'reason': 'INVALID_EQUITY_OR_PRICE',
            'quantity': 0.0, 'target_notional_pct': target_pct,
            'target_notional_usdt': 0.0, 'minimum_executable_qty': 0.0,
            'minimum_executable_notional_usdt': 0.0,
            'minimum_executable_notional_pct': 0.0,
            'allocation_unit': 'TARGET_NOTIONAL_PCT_OF_EQUITY',
        }
    step = float(filters.get('step_size', 0.001))
    min_qty = float(filters.get('min_qty', step))
    min_notional = float(filters.get('min_notional', 0.0))
    target_notional = equity * target_pct / 100.0
    qty = floor_to_step(target_notional / price, step)
    required_qty = max(min_qty, min_notional / price if min_notional else 0.0)
    required_qty = math.ceil(required_qty / step) * step
    required_qty = floor_to_step(required_qty, step)
    executable = bool(qty >= required_qty and qty > 0.0)
    minimum_notional = required_qty * price
    return {
        'executable': executable,
        'reason': 'PASS' if executable else 'VENUE_MINIMUM_EXCEEDS_ALLOCATION',
        'quantity': floor_to_step(qty, step) if executable else 0.0,
        'target_notional_pct': target_pct,
        'target_notional_usdt': target_notional,
        'minimum_executable_qty': required_qty,
        'minimum_executable_notional_usdt': minimum_notional,
        'minimum_executable_notional_pct': minimum_notional / equity * 100.0,
        'allocation_unit': 'TARGET_NOTIONAL_PCT_OF_EQUITY',
    }


def calculate_qty(balance_usdt, size_pct, current_price, filters):
    """Respect requested notional/equity allocation; never upsize to minimum."""
    return quantity_feasibility(
        balance_usdt, size_pct, current_price, filters
    )['quantity']


def calculate_early_protection_qty(
    current_qty, initial_qty, protection_closed_qty, requested_qty, step,
):
    """Cap cumulative early-protection exits and reserve half for SL/normal exits."""
    current = max(0.0, float(current_qty or 0.0))
    basis = max(0.0, float(initial_qty or 0.0))
    already = max(0.0, float(protection_closed_qty or 0.0))
    requested = max(0.0, float(requested_qty or 0.0))
    step = max(0.0, float(step or 0.0))
    if current <= 0.0 or basis <= 0.0 or requested <= 0.0:
        return 0.0
    maximum_early_close = basis * MAX_EARLY_PROTECTION_FRACTION
    minimum_sl_reserve = basis * (1.0 - MAX_EARLY_PROTECTION_FRACTION)
    quota_remaining = max(0.0, maximum_early_close - already)
    reserve_room = max(0.0, current - minimum_sl_reserve)
    allowed = min(current, requested, quota_remaining, reserve_room)
    return floor_to_step(allowed, step) if step > 0.0 else allowed


def calculate_tp1_close_qty(
    current_qty, initial_qty, allocation, current_price, filters,
):
    """Round an adaptive 0-70% TP1 leg without converting it to a full exit."""
    filters = dict(filters or {})
    step = max(float(filters.get('step_size', 0.001) or 0.001), 1e-12)
    min_qty = max(float(filters.get('min_qty', step) or step), step)
    min_notional = max(float(filters.get('min_notional', 0.0) or 0.0), 0.0)
    fraction = max(0.0, min(0.70, float(allocation or 0.0)))
    wanted = min(
        max(0.0, float(current_qty or 0.0)),
        max(0.0, float(initial_qty or 0.0)) * fraction,
    )
    quantity = floor_to_step(wanted, step)
    executable = bool(
        fraction > 0.0 and quantity >= min_qty
        and (not min_notional or quantity * float(current_price or 0.0) >= min_notional)
    )
    return {
        'executable': executable,
        'quantity': quantity if executable else 0.0,
        'requested_allocation': fraction,
        'reason': 'PASS' if executable else 'TP1_ALLOCATION_BELOW_VENUE_MINIMUM',
    }


def _favorable_target(side, entry_price, target, tick_size):
    target = float(target or 0.0)
    if side == 'LONG':
        return target if target >= entry_price + tick_size else 0.0
    return target if 0.0 < target <= entry_price - tick_size else 0.0


def _select_structure_targets(
    entry_price, side, tick_size, atr, poc, vah, val, swing_target,
    mode='', setup_zone=None, setup_kind=None, breakout_target=None,
    breakout_target2=None, breakout_target_basis=None,
):
    """Build a favorable target ladder from the zone that armed the setup.

    Pullbacks at POC target the opposite value-area edge. Pullbacks/fades at an
    outer edge first target POC. The explicit setup zone is authoritative; the
    entry-price proximity fallback only exists for restored/legacy signals.
    """
    try:
        zone = float(setup_zone or 0.0)
    except (TypeError, ValueError):
        zone = 0.0
    kind = str(setup_kind or '').lower()
    is_pullback = mode in ('TREND-PULLBACK', 'TRANSITION-PULLBACK')
    is_neutral_fade = mode == 'NEUTRAL-FADE'
    structure_aware = (is_pullback and kind != 'breakout') or is_neutral_fade

    if kind == 'breakout':
        tp1 = _favorable_target(side, entry_price, breakout_target, tick_size)
        tp2 = _favorable_target(side, tp1, breakout_target2, tick_size) if tp1 else 0.0
        if tp1 > 0.0:
            if tp2 <= 0.0:
                tp2 = tp1 + atr if side == 'LONG' else tp1 - atr
            return tp1, tp2, (
                breakout_target_basis or 'BREAKOUT_MEANINGFUL_LIQUIDITY_TARGET'
            )
        # This fallback keeps level geometry deterministic for shadow records.
        # Economic policy must reject it; ATR is not evidence of realizable edge.
        distance = max(1.5 * atr, tick_size)
        tp1 = entry_price + distance if side == 'LONG' else entry_price - distance
        tp2 = tp1 + atr if side == 'LONG' else tp1 - atr
        return tp1, tp2, 'BREAKOUT_TECHNICAL_ATR_FALLBACK_NO_ECONOMIC_TARGET'

    # Without zone metadata a restored/legacy signal is ambiguous. Preserve its
    # old POC-first behavior instead of guessing that it originated at POC and
    # silently moving TP1 to the far value-area edge.
    at_poc = bool(
        is_pullback
        and poc > 0.0
        and zone > 0.0
        and abs(zone - poc) <= max(2.0 * tick_size, 0.10 * atr)
    )

    if is_neutral_fade:
        preferred = poc
        basis = 'NEUTRAL_OUTER_TO_POC'
    elif is_pullback and kind != 'breakout' and zone <= 0.0:
        preferred = poc
        basis = 'PULLBACK_ZONE_UNKNOWN_TO_POC'
    elif is_pullback and kind != 'breakout':
        if at_poc:
            preferred = vah if side == 'LONG' else val
            basis = 'PULLBACK_POC_TO_VAH' if side == 'LONG' else 'PULLBACK_POC_TO_VAL'
        else:
            preferred = poc
            basis = 'PULLBACK_OUTER_TO_POC'
    else:
        preferred = poc
        basis = 'LEGACY_POC_OR_ATR'

    if side == 'LONG':
        fallback_candidates = (preferred, poc, vah, swing_target)
    else:
        fallback_candidates = (preferred, poc, val, swing_target)
    tp1 = next(
        (
            candidate for candidate in (
                _favorable_target(side, entry_price, value, tick_size)
                for value in fallback_candidates
            )
            if candidate > 0.0
        ),
        0.0,
    )
    if tp1 <= 0.0:
        distance = max(1.5 * atr, tick_size)
        tp1 = entry_price + distance if side == 'LONG' else entry_price - distance
        basis += '_ATR_FALLBACK'

    # Once POC is reached from an outer zone, the other value-area edge is the
    # natural second checkpoint. A POC-origin trade already uses that edge as
    # TP1, so its next target remains the M15 swing.
    value_edge = vah if side == 'LONG' else val
    if structure_aware and _favorable_target(side, tp1, value_edge, tick_size):
        tp2 = value_edge
    else:
        tp2 = _favorable_target(side, tp1, swing_target, tick_size)
    if tp2 <= 0.0:
        tp2 = tp1 + atr if side == 'LONG' else tp1 - atr
    return tp1, tp2, basis


def _slippage_estimate_bps(state, side):
    """Ước tính slippage (bps) dựa trên depth bids_top_10/asks_top_10 trong RAM.

    Args:
        state: MarketState object có thuộc tính bids_top_10, asks_top_10, atr_1m
        side: 'LONG' hoặc 'SHORT'
    Returns:
        float: slippage ước tính tính bằng bps (basis points)
    """
    try:
        # Thoát Long -> market sell -> cần xem depth bids
        # Thoát Short -> market buy -> cần xem depth asks
        if side == 'LONG':
            depth = getattr(state, 'bids_top_10', None)
        else:
            depth = getattr(state, 'asks_top_10', None)

        if not depth:
            return 5.0  # conservative default khi không có depth data

        # Lấy top 5 levels
        top5 = depth[:5]
        available_depth_usd = sum(price * qty for price, qty in top5)

        atr = getattr(state, 'atr_1m', None)
        if atr is None or atr <= 0:
            return 5.0

        # Lấy mid price từ depth level đầu tiên
        price = top5[0][0] if top5 else 0
        if price <= 0:
            return 5.0

        # Ước tính size lệnh Market SL ~0.1 BTC equiv
        order_usd = atr * 0.1 * price
        depth_consumption = min(order_usd / max(available_depth_usd, 1.0), 1.0)

        # Tuyến tính, tối đa 80bps
        slippage_bps = min(80.0, depth_consumption * 80.0)
        return slippage_bps
    except Exception:
        return 5.0  # fallback conservative default


def calculate_levels(
    state, entry_price, side, tick_size, mode='', setup_zone=None,
    setup_kind=None, breakout_target=None, breakout_target2=None,
    breakout_target_basis=None, evaluation_time=None, exit_plan=None,
):
    atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
    if atr <= 0:
        raise ValueError('ATR chưa sẵn sàng')
    poc = float(getattr(state, 'poc', 0.0) or 0.0)
    vah = float(getattr(state, 'vah', 0.0) or 0.0)
    val = float(getattr(state, 'val', 0.0) or 0.0)
    sweep = getattr(state, 'sweep_m1', {})
    evaluation_time = time.time() if evaluation_time is None else float(evaluation_time)
    sweep_fresh = 0.0 <= evaluation_time - sweep.get('ts', 0.0) <= 120.0
    hard_distance = max(3.0 * atr, MIN_INITIAL_SOFT_SL_DISTANCE + tick_size)

    if side == 'LONG':
        hard_sl = entry_price - hard_distance
        if sweep_fresh and sweep.get('direction') == 'LONG' and sweep.get('level', 0) > 0:
            soft_sl = sweep['level'] - 0.1 * atr
        elif mode == 'NEUTRAL-FADE' and val > 0:
            soft_sl = val - 0.5 * atr
        else:
            zone_floor = min(x for x in (poc, val) if x > 0) if (poc > 0 or val > 0) else entry_price
            soft_sl = zone_floor - 0.5 * atr
        structural_sl = soft_sl
        # Cho vị thế đủ khoảng thở ban đầu: Soft SL cách entry ít nhất 41 giá,
        # nhưng vẫn phải nằm bên trong Hard SL.
        soft_sl = min(soft_sl, entry_price - MIN_INITIAL_SOFT_SL_DISTANCE)
        soft_sl = max(soft_sl, hard_sl + tick_size)
        if exit_plan and exit_plan.get('available'):
            soft_tp1 = float(exit_plan.get('tp1', 0.0) or 0.0)
            soft_tp2 = float(exit_plan.get('runner_target', 0.0) or 0.0)
            target_basis = str(exit_plan.get('version') or 'DYNAMIC_PATH_V2')
        else:
            soft_tp1, soft_tp2, target_basis = _select_structure_targets(
                entry_price, side, tick_size, atr, poc, vah, val,
                float(getattr(state, 'swing_high_m15', 0.0) or 0.0),
                mode=mode, setup_zone=setup_zone, setup_kind=setup_kind,
                breakout_target=breakout_target,
                breakout_target2=breakout_target2,
                breakout_target_basis=breakout_target_basis,
            )
    else:
        hard_sl = entry_price + hard_distance
        if sweep_fresh and sweep.get('direction') == 'SHORT' and sweep.get('level', 0) > 0:
            soft_sl = sweep['level'] + 0.1 * atr
        elif mode == 'NEUTRAL-FADE' and vah > 0:
            soft_sl = vah + 0.5 * atr
        else:
            zone_ceiling = max(poc, vah) if (poc > 0 or vah > 0) else entry_price
            soft_sl = zone_ceiling + 0.5 * atr
        structural_sl = soft_sl
        soft_sl = max(soft_sl, entry_price + MIN_INITIAL_SOFT_SL_DISTANCE)
        soft_sl = min(soft_sl, hard_sl - tick_size)
        if exit_plan and exit_plan.get('available'):
            soft_tp1 = float(exit_plan.get('tp1', 0.0) or 0.0)
            soft_tp2 = float(exit_plan.get('runner_target', 0.0) or 0.0)
            target_basis = str(exit_plan.get('version') or 'DYNAMIC_PATH_V2')
        else:
            soft_tp1, soft_tp2, target_basis = _select_structure_targets(
                entry_price, side, tick_size, atr, poc, vah, val,
                float(getattr(state, 'swing_low_m15', 0.0) or 0.0),
                mode=mode, setup_zone=setup_zone, setup_kind=setup_kind,
                breakout_target=breakout_target,
                breakout_target2=breakout_target2,
                breakout_target_basis=breakout_target_basis,
            )

    spread = max(
        0.0,
        float(getattr(state, 'best_ask', 0.0) or 0.0)
        - float(getattr(state, 'best_bid', 0.0) or 0.0),
    )
    guardian_policy = adaptive_guardian_policy(
        entry_price, structural_sl, soft_tp2, atr, spread, tick_size,
        noise_scale=float(getattr(state, 'side_noise_scale', 1.0) or 1.0),
    )
    if guardian_policy['mode'] == 'BOUNDED_LIVE':
        reserve = guardian_policy['stop_distance']
        soft_sl = entry_price - reserve if side == 'LONG' else entry_price + reserve

    # [SLIPPAGE BUFFER] Nới Hard SL theo depth thực tế để tránh SL thực tế tệ hơn tính toán
    _slip_bps = _slippage_estimate_bps(state, side)
    _slip_abs = (entry_price * _slip_bps / 10000.0)
    if side == 'LONG':
        hard_sl = hard_sl - _slip_abs
    else:
        hard_sl = hard_sl + _slip_abs
    rounded_hard_sl = round_to_tick(hard_sl, tick_size)
    rounded_soft_sl = round_to_tick(soft_sl, tick_size)
    if side == 'LONG':
        rounded_soft_sl = min(
            rounded_soft_sl,
            floor_to_step(entry_price - MIN_INITIAL_SOFT_SL_DISTANCE, tick_size),
        )
        if rounded_hard_sl >= rounded_soft_sl:
            rounded_hard_sl = floor_to_step(
                rounded_soft_sl - tick_size, tick_size
            )
    else:
        rounded_soft_sl = max(
            rounded_soft_sl,
            ceil_to_step(entry_price + MIN_INITIAL_SOFT_SL_DISTANCE, tick_size),
        )
        if rounded_hard_sl <= rounded_soft_sl:
            rounded_hard_sl = ceil_to_step(
                rounded_soft_sl + tick_size, tick_size
            )

    return {
        'hard_sl': rounded_hard_sl,
        'soft_sl': rounded_soft_sl,
        'soft_tp1': round_to_tick(soft_tp1, tick_size),
        'soft_tp2': round_to_tick(soft_tp2, tick_size),
        'target_basis': target_basis,
        'exit_plan_version': (
            exit_plan.get('version') if isinstance(exit_plan, dict) else None
        ),
        'guardian_policy': guardian_policy,
    }
