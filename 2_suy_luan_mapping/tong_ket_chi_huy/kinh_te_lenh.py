"""Tách chi phí khớp lệnh khỏi kỳ vọng capture; policy cũ vẫn được enforce khi shadow học."""

import os


ENTRY_FEE_BPS = float(os.getenv('SMC_SHADOW_ENTRY_FEE_BPS', '4.0'))
PASSIVE_ENTRY_FEE_BPS = float(
    os.getenv('SMC_PASSIVE_ENTRY_FEE_BPS', '2.0')
)
EXIT_FEE_BPS = float(os.getenv('SMC_SHADOW_EXIT_FEE_BPS', '4.0'))
MINIMUM_NET_EDGE_BPS = float(os.getenv('SMC_MIN_NET_EDGE_BPS', '4.0'))
FALLBACK_CAPTURE_RATIO = float(os.getenv('SMC_CAPTURE_RATIO', '0.60'))


def executable_price(side, best_bid, best_ask, closing=False):
    """Giá chạm được ngay: ask khi mua, bid khi bán."""
    is_buy = (side == 'LONG') != bool(closing)
    return float(best_ask if is_buy else best_bid)


def estimate_market_fill(levels, quantity):
    """Walk Top-10 depth; thiếu depth thì trả unavailable thay vì bịa fill."""
    quantity = float(quantity or 0.0)
    clean = [
        (float(price), float(qty))
        for price, qty in (levels or [])
        if float(price) > 0.0 and float(qty) > 0.0
    ]
    if quantity <= 0.0 or not clean:
        return {
            'available': False, 'reason': 'NO_DEPTH_OR_QTY',
            'requested_qty': quantity, 'filled_qty': 0.0,
        }

    remaining = quantity
    quote = 0.0
    fills = []
    for price, level_qty in clean:
        take = min(remaining, level_qty)
        if take <= 0.0:
            continue
        fills.append({'price': price, 'qty': take})
        quote += price * take
        remaining -= take
        if remaining <= 1e-12:
            break

    filled = quantity - max(0.0, remaining)
    if remaining > 1e-12:
        return {
            'available': False, 'reason': 'TOP10_DEPTH_INSUFFICIENT',
            'requested_qty': quantity, 'filled_qty': filled, 'fills': fills,
        }
    average = quote / filled
    reference = clean[0][0]
    slippage_bps = abs(average - reference) / reference * 10000.0
    return {
        'available': True, 'reason': 'TOP10_EXECUTABLE',
        'requested_qty': quantity, 'filled_qty': filled,
        'avg_price': average, 'reference_price': reference,
        'slippage_bps': slippage_bps, 'fills': fills,
    }


def observe_snapshot(
    side, quantity, soft_tp1, bids_top_10, asks_top_10,
    best_bid, best_ask, capture_ratio=None,
    setup_kind=None, target_basis=None, entry_style=None, exit_plan=None,
):
    """Pure economics từ một immutable decision snapshot."""
    side = str(side)
    entry_levels = asks_top_10 if side == 'LONG' else bids_top_10
    exit_levels = bids_top_10 if side == 'LONG' else asks_top_10
    entry = estimate_market_fill(entry_levels, quantity)
    exit_estimate = estimate_market_fill(exit_levels, quantity)
    if exit_plan is not None:
        plan = dict(exit_plan or {})
        available = bool(plan.get('available'))
        edge_lcb = plan.get('realizable_edge_lcb', plan.get('net_edge_lcb'))
        edge_mean = plan.get('net_edge_mean')
        passed = bool(
            available and edge_lcb is not None and float(edge_lcb) > 0.0
            and entry.get('available') and exit_estimate.get('available')
        )
        reason = (
            'PASS' if passed else plan.get('reason')
            or 'DYNAMIC_PATH_UNAVAILABLE'
        )
        return {
            'mode': 'DYNAMIC_PATH_ENFORCED',
            'blocks_entry': True,
            'projected_gate_mode': 'DYNAMIC_PATH_LCB',
            'structural_fee_floor_mode': 'DYNAMIC_PATH_LCB',
            'execution_floor_mode': 'ENFORCED_IN_PLAN',
            'calibration_stage': 'PATH_PRIOR_V1_LIVE_TESTNET',
            'model': plan.get('model_version', 'PATH_PRIOR_V1'),
            'feature_schema': plan.get('feature_schema'),
            'entry_fee_bps': plan.get('entry_fee_bps', ENTRY_FEE_BPS),
            'expected_exit_fee_bps': plan.get('exit_fee_bps', EXIT_FEE_BPS),
            'minimum_net_edge_bps': 0.0,
            'entry_fill_estimate': entry,
            'exit_fill_estimate': exit_estimate,
            'setup_kind': setup_kind,
            'target_basis': target_basis,
            'entry_style': entry_style,
            'economic_pass': passed,
            'structural_fee_floor_pass': passed,
            'structural_fee_floor_reason': reason,
            'structural_fee_floor_basis': 'FULL_EXIT_PLAN_NET_EDGE_LCB',
            'execution_floor_pass': bool(
                entry.get('available') and exit_estimate.get('available')
            ),
            'execution_floor_reason': (
                'PASS' if entry.get('available') and exit_estimate.get('available')
                else 'INSUFFICIENT_EXECUTABLE_DEPTH'
            ),
            'expected_edge_pass': passed,
            'expected_edge_basis': 'DYNAMIC_PATH_REALIZABLE_LCB',
            'reason': reason,
            'tp1_distance_bps': next((
                item.get('distance_bps')
                for item in plan.get('target_candidates', ())
                if item.get('target_id') == (plan.get('selected_target_ids') or [None])[0]
            ), None),
            'projected_capture_bps': (
                float(edge_mean) + float(plan.get('all_in_cost_bps', 0.0))
                if edge_mean is not None else None
            ),
            'all_in_cost_bps': plan.get('all_in_cost_bps'),
            'required_capture_bps': plan.get('all_in_cost_bps'),
            # Compatibility field means conservative edge under V2.
            'expected_net_edge_bps': edge_lcb,
            'net_edge_mean': edge_mean,
            'net_edge_lcb': plan.get('net_edge_lcb'),
            'realizable_edge_lcb': edge_lcb,
            'realizable_capture_lcb_bps': plan.get(
                'realizable_capture_lcb_bps'
            ),
            'checkpoint_monetizable': plan.get('checkpoint_monetizable'),
            'checkpoint_lock_net_bps': plan.get('checkpoint_lock_net_bps'),
            'entry_policy': plan.get('entry_policy'),
            'economic_size_multiplier': plan.get('economic_size_multiplier'),
            'exit_plan': plan,
        }
    ratio = FALLBACK_CAPTURE_RATIO if capture_ratio is None else float(capture_ratio)
    ratio = min(1.0, max(0.0, ratio))

    result = {
        'mode': 'ENFORCED_NET_EDGE',
        'blocks_entry': True,
        'projected_gate_mode': 'ENFORCED_NET_EDGE',
        'structural_fee_floor_mode': 'ENFORCED_NET_EDGE',
        'execution_floor_mode': 'OBSERVE_ONLY',
        'calibration_stage': 'STATIC_FALLBACK_ENFORCED_COLLECTING_SHADOW',
        'model': 'TP1_DISTANCE_X_CAPTURE_RATIO_V1',
        'capture_ratio': ratio,
        # Chưa có calibrator chạy thật; ghi đúng bản chất thay vì giả vờ đã có
        # đường học từ sample.
        'capture_ratio_source': 'STATIC_FALLBACK_PENDING_SHADOW_CALIBRATION',
        'entry_fee_bps': ENTRY_FEE_BPS,
        'expected_exit_fee_bps': EXIT_FEE_BPS,
        'minimum_net_edge_bps': MINIMUM_NET_EDGE_BPS,
        'entry_fill_estimate': entry,
        'exit_fill_estimate': exit_estimate,
        'setup_kind': setup_kind,
        'target_basis': target_basis,
        'entry_style': entry_style,
    }
    no_breakout_target = bool(
        str(setup_kind or '').lower() == 'breakout'
        and (
            not target_basis
            or 'NO_ECONOMIC_TARGET' in str(target_basis)
            or 'NO_MEANINGFUL' in str(target_basis)
        )
    )
    if no_breakout_target:
        result.update({
            'economic_pass': False,
            'structural_fee_floor_pass': False,
            'structural_fee_floor_reason': 'NO_MEANINGFUL_LIQUIDITY_TARGET',
            'reason': 'NO_MEANINGFUL_LIQUIDITY_TARGET',
            'projected_capture_bps': 0.0,
            'all_in_cost_bps': None,
            'required_capture_bps': None,
            'expected_net_edge_bps': None,
            'execution_floor_pass': False,
            'execution_floor_reason': 'NO_MEANINGFUL_LIQUIDITY_TARGET',
        })
        return result
    if not entry.get('available') or not exit_estimate.get('available'):
        result.update({
            'economic_pass': None,
            'structural_fee_floor_pass': False,
            'structural_fee_floor_reason': 'INSUFFICIENT_EXECUTABLE_DEPTH',
            'reason': 'INSUFFICIENT_EXECUTABLE_DEPTH',
            'projected_capture_bps': None,
            'all_in_cost_bps': None,
            'required_capture_bps': None,
            'expected_net_edge_bps': None,
            'execution_floor_pass': False,
            'execution_floor_reason': 'INSUFFICIENT_EXECUTABLE_DEPTH',
            'execution_floor_required_bps': None,
            'execution_uncertainty_bps': None,
        })
        return result

    entry_price = float(entry['avg_price'])
    tp1 = float(soft_tp1 or 0.0)
    favorable = tp1 - entry_price if side == 'LONG' else entry_price - tp1
    tp1_distance_bps = max(0.0, favorable / entry_price * 10000.0)
    projected = tp1_distance_bps * ratio
    best_bid = float(best_bid or 0.0)
    best_ask = float(best_ask or 0.0)
    mid = (best_bid + best_ask) / 2.0 if best_ask > best_bid > 0.0 else 0.0
    spread_bps = (best_ask - best_bid) / mid * 10000.0 if mid > 0.0 else 0.0
    entry_slippage = float(entry.get('slippage_bps', 0.0))
    exit_slippage = float(exit_estimate.get('slippage_bps', 0.0))
    all_in = (
        ENTRY_FEE_BPS + EXIT_FEE_BPS
        + entry_slippage + exit_slippage
    )
    # Spread không bị trộn vào capture ratio. Dùng nó như uncertainty quan sát
    # độc lập bên cạnh depth-walk; chưa enforce cho tới khi shadow đủ mẫu.
    execution_uncertainty = spread_bps
    execution_floor_required = all_in + execution_uncertainty
    execution_floor_pass = tp1_distance_bps >= execution_floor_required
    required = all_in + MINIMUM_NET_EDGE_BPS
    expected_net = projected - all_in
    result.update({
        'economic_pass': projected >= required,
        # Executor hard-block trường này. Phải so phần kỳ vọng thực sự thu được,
        # không được dùng khoảng TP1 raw rồi bỏ quên capture ratio.
        'structural_fee_floor_pass': projected >= required,
        'structural_fee_floor_reason': (
            'PASS'
            if projected >= required
            else 'PROJECTED_CAPTURE_BELOW_ALL_IN_COST_PLUS_EDGE'
        ),
        'structural_fee_floor_basis': 'TP1_DISTANCE_X_CAPTURE_RATIO',
        'execution_floor_pass': execution_floor_pass,
        'execution_floor_reason': (
            'PASS'
            if execution_floor_pass
            else 'RAW_TP1_BELOW_FEES_DEPTH_AND_SPREAD'
        ),
        'execution_floor_basis': 'RAW_TP1_VS_FEES_DEPTH_SLIPPAGE_AND_SPREAD',
        'execution_floor_required_bps': execution_floor_required,
        'execution_uncertainty_bps': execution_uncertainty,
        'current_spread_bps': spread_bps,
        'expected_edge_pass': projected >= required,
        'expected_edge_basis': 'STATIC_CAPTURE_PENDING_CALIBRATION',
        'reason': (
            'PASS'
            if projected >= required
            else 'PROJECTED_CAPTURE_BELOW_ALL_IN_COST_PLUS_EDGE'
        ),
        'tp1_distance_bps': tp1_distance_bps,
        'projected_capture_bps': projected,
        'expected_entry_slippage_bps': entry_slippage,
        'expected_exit_slippage_bps': exit_slippage,
        'all_in_cost_bps': all_in,
        'required_capture_bps': required,
        'expected_net_edge_bps': expected_net,
    })
    return result


def observe(
    state, side, quantity, soft_tp1, capture_ratio=None,
    setup_kind=None, target_basis=None, entry_style=None, exit_plan=None,
):
    """Live compatibility wrapper; policy/fee gate giữ nguyên hoàn toàn."""
    return observe_snapshot(
        side, quantity, soft_tp1,
        getattr(state, 'bids_top_10', ()),
        getattr(state, 'asks_top_10', ()),
        getattr(state, 'best_bid', 0.0),
        getattr(state, 'best_ask', 0.0),
        capture_ratio=capture_ratio,
        setup_kind=setup_kind,
        target_basis=target_basis,
        entry_style=entry_style,
        exit_plan=exit_plan,
    )
