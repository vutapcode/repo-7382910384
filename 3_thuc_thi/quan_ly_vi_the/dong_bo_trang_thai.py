"""ROM atomic backup và reconciliation với Binance (exchange là sự thật cuối cùng)."""

import asyncio
import importlib.util
import json
import logging
import os
import tempfile
import time
from pathlib import Path

try:
    from loi_he_thong.order_identity import client_order_id as forensic_order_id
except ModuleNotFoundError:
    _identity_spec = importlib.util.spec_from_file_location(
        'reconcile_order_identity',
        Path(__file__).resolve().parents[2] / 'loi_he_thong' / 'order_identity.py',
    )
    _identity_mod = importlib.util.module_from_spec(_identity_spec)
    _identity_spec.loader.exec_module(_identity_mod)
    forensic_order_id = _identity_mod.client_order_id


ROM_PATH = Path(__file__).resolve().parent / 'rom_backup' / 'vi_the.json'


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


journal_mod = _load_module(
    'nhat_ky_giao_dich_reconcile',
    Path(__file__).resolve().parent / 'nhat_ky_giao_dich.py',
)


POSITION_FIELDS = (
    'active', 'symbol', 'side', 'qty', 'initial_qty', 'protection_closed_qty',
    'protection_reasons_done', 'entry_price',
    'strategy_entry_price', 'execution_entry_price', 'hard_sl', 'strategy_hard_sl',
    'soft_sl', 'soft_tp1', 'soft_tp2', 'tp1_allocation', 'runner_policy',
    'tp1_checkpoint_monetizable', 'tp1_checkpoint_lock_net_bps',
    'sl_order_id', 'hard_sl_algo_id',
    'hard_sl_client_algo_id', 'opened_at', 'tp1_done', 'trailing_active',
    'split_sl_enabled', 'split_sl1_done', 'split_sl1_fraction',
    'split_sl1', 'split_sl2', 'standard_hard_sl',
    'add_on_done', 'add_on_attempted', 'mode',
    'setup_id', 'setup_semantic_key', 'opportunity_id', 'setup_zone',
    'venue_price_offset',
    'setup_generation',
    'tp2_extended', 'shark_adverse_since',
    'guardian_policy', 'strategy_profile', 'entry_continuous_score',
    'dynamic_exit_plan', 'breakout_target', 'breakout_target2',
    'shark_support_since', 'position_cycle_id', 'entry_order_id',
    'entry_client_order_id',
)


def _snapshot(position):
    return {name: getattr(position, name, None) for name in POSITION_FIELDS}


def _atomic_write(data, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='vi_the_', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _read_rom():
    try:
        with open(ROM_PATH, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _restore_fields(position, data):
    for name in POSITION_FIELDS:
        if name in data and data[name] is not None:
            setattr(position, name, data[name])


def _clear_local(state):
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
    for name, value in {
        'active': False, 'side': '', 'qty': 0.0, 'initial_qty': 0.0,
        'protection_closed_qty': 0.0, 'protection_reasons_done': [],
        'entry_price': 0.0, 'strategy_entry_price': 0.0,
        'execution_entry_price': 0.0, 'hard_sl': 0.0,
        'strategy_hard_sl': 0.0, 'soft_sl': 0.0,
        'soft_tp1': 0.0, 'soft_tp2': 0.0, 'sl_order_id': None,
        'tp1_allocation': 0.50, 'runner_policy': 'LEGACY_TP2',
        'tp1_checkpoint_monetizable': False,
        'tp1_checkpoint_lock_net_bps': 0.0,
        'hard_sl_algo_id': None, 'hard_sl_client_algo_id': None,
        'split_sl_enabled': False, 'split_sl1_done': False,
        'split_sl1_fraction': 0.90, 'split_sl1': 0.0, 'split_sl2': 0.0,
        'standard_hard_sl': 0.0,
        'opened_at': 0.0, 'tp1_done': False, 'trailing_active': False,
        'add_on_done': False, 'add_on_attempted': False, 'mode': '',
        'setup_id': '', 'setup_generation': 0, 'tp2_extended': False,
        'setup_semantic_key': '', 'opportunity_id': '', 'setup_zone': 0.0,
        'venue_price_offset': 0.0,
        'shark_adverse_since': 0.0, 'shark_support_since': 0.0,
        'guardian_policy': {}, 'strategy_profile': '',
        'entry_continuous_score': {}, 'dynamic_exit_plan': {},
        'breakout_target': 0.0, 'breakout_target2': 0.0,
        'position_cycle_id': '', 'entry_order_id': None,
        'entry_client_order_id': None,
    }.items():
        setattr(position, name, value)
    state.co_lenh_mo = False
    state.pending_close = None


def _matching_hard_sls(open_algos, side):
    matches = []
    for order in open_algos if isinstance(open_algos, list) else []:
        order_type = order.get('type', order.get('orderType', ''))
        position_side = order.get('positionSide', 'BOTH')
        if order_type == 'STOP_MARKET' and position_side in (side, 'BOTH'):
            matches.append(order)
    return matches


def _find_by_client_id(open_algos, client_algo_id):
    if not isinstance(open_algos, list):
        return None
    return next(
        (order for order in open_algos if order.get('clientAlgoId') == client_algo_id),
        None,
    )


def _clear_unknown_execution(state, executed=False):
    setup_id = getattr(state, 'execution_setup_id', None)
    generation = int(getattr(state, 'execution_generation', 0))
    matched_setup = None
    if executed:
        for setup in getattr(state, 'active_setups', {}).values():
            if (
                setup.get('setup_id') == setup_id
                and int(setup.get('generation', 0)) == generation
            ):
                setup['state'] = 'EXECUTED'
                matched_setup = setup
        if setup_id:
            state.setup_cooldowns[setup_id] = time.monotonic() + 60.0
            state.vi_the_hien_tai.setup_id = setup_id
            state.vi_the_hien_tai.setup_generation = generation
        if matched_setup is not None and matched_setup.get('semantic_key'):
            semantic_key = matched_setup['semantic_key']
            state.setup_cooldowns[semantic_key] = time.monotonic() + 60.0
            state.rearm_blocks[semantic_key] = {
                'zone': float(matched_setup.get('zone', 0.0) or 0.0),
                'blocked_at_mono': time.monotonic(),
            }
            state.vi_the_hien_tai.setup_semantic_key = semantic_key
            state.vi_the_hien_tai.setup_zone = float(
                matched_setup.get('zone', 0.0) or 0.0
            )
    state.execution_in_flight = False
    state.execution_setup_id = None
    state.execution_generation = 0
    state.execution_client_order_id = None
    state.execution_unknown = False
    state.execution_unknown_since = 0.0
    state.last_execution_release_mono = time.monotonic()


async def _cancel_orphan_smc_algos(api, open_algos):
    for order in open_algos if isinstance(open_algos, list) else []:
        client_id = str(order.get('clientAlgoId', ''))
        algo_id = order.get('algoId')
        if not client_id.startswith('smc_') or algo_id is None:
            continue
        result, status = await api.cancel_algo_order(algo_id)
        code = result.get('code') if isinstance(result, dict) else None
        if status != 200 and code != -2011:
            logging.error('❌ [RECONCILE] Không dọn được Algo mồ côi %s: %s', algo_id, result)
            return False
        logging.warning('🧹 [RECONCILE] Đã dọn Algo mồ côi %s', algo_id)
    return True


async def _sync_terminal_exchange_fills(state, api, cycle_id, position):
    """Attach partial + exchange SL fills before local position is cleared."""
    if not cycle_id or not hasattr(api, 'get_account_trades'):
        return None
    opened_at = float(getattr(position, 'opened_at', 0.0) or 0.0)
    start_ms = int(max(0.0, opened_at - 60.0) * 1000) if opened_at else None
    trades, status = await api.get_account_trades('BTCUSDT', start_time=start_ms)
    if status != 200:
        logging.error(
            '❌ [RECONCILE] Không tải được fills để đóng cycle %s.', cycle_id
        )
        return None
    cycle = getattr(state, 'trade_cycles', {}).get(cycle_id)
    if cycle is None:
        return None
    # SL2 is linked only by the exact exchange identity retained on position.
    # Never infer it from side/time/remaining quantity.
    algo_order_id = getattr(position, 'hard_sl_algo_id', None)
    known_ids = {
        str(order.get('order_id'))
        for order in cycle.get('actual', {}).get('orders', ())
        if order.get('order_id') is not None
    }
    matching = [
        trade for trade in trades if str(trade.get('orderId')) == str(algo_order_id)
    ] if algo_order_id is not None else []
    if matching and str(algo_order_id) not in known_ids:
        qty = sum(float(item.get('qty', 0.0) or 0.0) for item in matching)
        journal_mod.record_actual_order(
            state, cycle_id, 'SL2', {
                'orderId': algo_order_id,
                'clientOrderId': getattr(position, 'hard_sl_client_algo_id', None),
                'status': 'FILLED', 'executedQty': qty,
            }, qty, 0.0, reason='SL2_EXCHANGE_FILL',
        )
    journal_mod._apply_trade_fills(state, trades)
    if (
        cycle.get('status') == 'CLOSED'
        and getattr(position, 'split_sl_enabled', False)
        and getattr(position, 'split_sl1_done', False)
        and cycle.get('exit_reason') in (
            None, 'FILLS_RECONCILED_CLOSED', 'RECONCILE_INFERRED_EXCHANGE_CLOSE'
        )
    ):
        cycle['exit_reason'] = 'SL2_EXCHANGE_FILL'
    return cycle


async def _create_recovery_hard_sl(state, api, symbol, side, trigger_price):
    position = getattr(state, 'vi_the_hien_tai', None)
    client_id = forensic_order_id(
        state, 'RECOVER',
        opportunity_id=getattr(position, 'setup_semantic_key', None),
        setup_id=getattr(position, 'setup_id', None),
        generation=getattr(position, 'setup_generation', 0),
        nonce=int(time.time() * 1000),
    )
    params = {
        'symbol': symbol,
        'side': 'SELL' if side == 'LONG' else 'BUY',
        'type': 'STOP_MARKET',
        'triggerPrice': trigger_price,
        'workingType': 'MARK_PRICE',
        'closePosition': 'true',
        'clientAlgoId': client_id,
    }
    if state.account_hedge_mode:
        params['positionSide'] = side

    last_result = None
    for attempt in range(5):
        result, status = await api.new_algo_order(**params)
        last_result = result
        if status == 200 and isinstance(result, dict) and result.get('algoId'):
            return result
        # POST có thể thành công nhưng mất response: tìm đúng id trước khi retry.
        open_algos, open_status = await api.get_open_algo_orders(symbol)
        recovered = _find_by_client_id(open_algos, client_id) if open_status == 200 else None
        if recovered is not None:
            return recovered
        if attempt < 4:
            await asyncio.sleep(0.2 * (attempt + 1))
    return last_result if isinstance(last_result, dict) else {'message': str(last_result)}


async def reconcile_once(state, api, symbol='BTCUSDT'):
    if getattr(state, 'dang_xu_ly_dong_lenh', False):
        return getattr(state, 'reconcile_ready', False)
    positions, status = await api.get_positions(symbol)
    if status != 200:
        state.reconcile_ready = False
        state.last_readiness_reason = 'Không đọc được vị thế từ sàn'
        return False

    active = [p for p in positions if abs(float(p.get('positionAmt', 0.0))) > 0]
    if len(active) > 1:
        state.reconcile_ready = False
        state.last_readiness_reason = 'Có đồng thời nhiều vị thế; cần can thiệp thủ công'
        return False

    if getattr(state, 'execution_unknown', False) and not active:
        client_id = getattr(state, 'execution_client_order_id', None)
        if not client_id:
            state.reconcile_ready = False
            state.last_readiness_reason = 'Entry mơ hồ thiếu clientOrderId'
            return False
        order, order_status = await api.query_order(symbol, client_id)
        order_state = order.get('status') if isinstance(order, dict) else None
        unknown_age = time.time() - float(getattr(state, 'execution_unknown_since', 0.0) or 0.0)
        if order_status == 200 and order_state in ('REJECTED', 'EXPIRED', 'CANCELED'):
            _clear_unknown_execution(state, executed=False)
        elif order_status == 200 and order_state == 'FILLED' and unknown_age >= 15.0:
            # Market entry đã fill nhưng hiện không còn vị thế: sàn đã đóng nó trước vòng reconcile.
            _clear_unknown_execution(state, executed=True)
        elif order_status == 200:
            state.reconcile_ready = False
            state.last_readiness_reason = f'Entry {client_id} đang ở trạng thái {order_state}'
            return False
        else:
            code = order.get('code') if isinstance(order, dict) else None
            if code == -2013 and unknown_age >= 15.0:
                logging.warning('🔍 [RECONCILE] Xác nhận entry %s không tồn tại.', client_id)
                _clear_unknown_execution(state, executed=False)
            else:
                state.reconcile_ready = False
                state.last_readiness_reason = f'Chưa xác minh được entry {client_id}'
                return False

    if not active:
        if state.vi_the_hien_tai.active:
            logging.warning('🔄 [RECONCILE] Sàn không còn vị thế; dọn state local.')
            position = state.vi_the_hien_tai
            cycle_id = getattr(position, 'position_cycle_id', '')
            synced_cycle = await _sync_terminal_exchange_fills(
                state, api, cycle_id, position
            )
            pending = getattr(state, 'pending_close', None)
            if cycle_id and pending:
                role = 'TP1' if pending.get('tag') == 'tp1' else 'CLOSE'
                result = pending.get('order_result') or {
                    'clientOrderId': pending.get('client_order_id'),
                    'orderId': pending.get('order_id'),
                    'status': 'FILLED_RECOVERED_FROM_POSITION',
                }
                journal_mod.record_actual_order(
                    state, cycle_id, role, result,
                    pending.get('requested_qty', position.qty),
                    pending.get('execution_reference_price', 0.0),
                    reason=pending.get('reason', 'RECONCILE_POSITION_GONE'),
                    strategy_reference_price=pending.get(
                        'strategy_trigger_price', pending.get('trigger_price', 0.0)
                    ),
                    execution_reference_price=pending.get(
                        'execution_reference_price', 0.0
                    ),
                )
            if cycle_id and not (
                synced_cycle and synced_cycle.get('status') == 'CLOSED'
            ):
                journal_mod.mark_actual_closed(
                    state, cycle_id,
                    (pending or {}).get('reason', 'RECONCILE_POSITION_GONE'),
                    (pending or {}).get(
                        'strategy_trigger_price',
                        (pending or {}).get('trigger_price', 0.0),
                    ),
                    result=(pending or {}).get('order_result'),
                    execution_reference_price=(pending or {}).get(
                        'execution_reference_price', 0.0
                    ),
                )
            elif synced_cycle:
                logging.info(
                    '✅ [RECONCILE] Cycle %s đã đóng từ fills (%s).',
                    cycle_id, synced_cycle.get('exit_reason'),
                )
            _clear_local(state)
        algos, algo_status = await api.get_open_algo_orders(symbol)
        if algo_status != 200:
            state.reconcile_ready = False
            state.last_readiness_reason = 'Không đọc được Algo orders để dọn orphan'
            return False
        if not await _cancel_orphan_smc_algos(api, algos):
            state.reconcile_ready = False
            state.last_readiness_reason = 'Không dọn được Algo order mồ côi'
            return False
        state.reconcile_ready = True
        return True

    exchange_position = active[0]
    side = exchange_position.get('positionSide')
    if side == 'BOTH':
        side = 'LONG' if float(exchange_position['positionAmt']) > 0 else 'SHORT'
    qty = abs(float(exchange_position['positionAmt']))
    entry = float(exchange_position.get('entryPrice', 0.0))
    position = state.vi_the_hien_tai

    if not position.active or position.side != side:
        rom = _read_rom()
        if rom.get('active') and rom.get('side') == side:
            _restore_fields(position, rom)
        position.active = True
        position.side = side
        position.opened_at = position.opened_at or time.time()
        logging.warning('🔄 [RECONCILE] Khôi phục vị thế %s %.4f @ %.2f', side, qty, entry)
    position.execution_entry_price = entry
    if float(getattr(position, 'strategy_entry_price', 0.0) or 0.0) <= 0:
        legacy_entry = float(getattr(position, 'entry_price', 0.0) or 0.0)
        legacy_offset = float(getattr(position, 'venue_price_offset', 0.0) or 0.0)
        mainnet_reference = (
            float(getattr(state, 'best_ask', 0.0) or 0.0)
            if side == 'LONG'
            else float(getattr(state, 'best_bid', 0.0) or 0.0)
        )
        strategy_entry = (
            legacy_entry - legacy_offset
            if legacy_entry > 0 and legacy_offset != 0
            else mainnet_reference
        )
        if strategy_entry <= 0:
            state.reconcile_ready = False
            state.last_readiness_reason = 'Không phục hồi được entry Mainnet'
            return False
        position.entry_price = strategy_entry
        position.strategy_entry_price = strategy_entry
    else:
        position.entry_price = position.strategy_entry_price
    position.venue_price_offset = position.execution_entry_price - position.entry_price
    position.qty = qty
    position.initial_qty = max(float(position.initial_qty or 0.0), qty)
    position.protection_closed_qty = max(
        0.0, float(getattr(position, 'protection_closed_qty', 0.0) or 0.0)
    )
    position.protection_reasons_done = list(
        getattr(position, 'protection_reasons_done', []) or []
    )
    state.co_lenh_mo = True

    atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
    if atr > 0:
        if position.hard_sl <= 0:
            position.hard_sl = entry - 3 * atr if side == 'LONG' else entry + 3 * atr
        if position.strategy_hard_sl <= 0:
            position.strategy_hard_sl = (
                position.entry_price - 3 * atr
                if side == 'LONG' else position.entry_price + 3 * atr
            )
        if position.soft_sl <= 0:
            position.soft_sl = entry - 0.5 * atr if side == 'LONG' else entry + 0.5 * atr
        if position.soft_tp1 <= 0:
            position.soft_tp1 = entry + 1.5 * atr if side == 'LONG' else entry - 1.5 * atr
        if position.soft_tp2 <= 0:
            position.soft_tp2 = entry + 2.5 * atr if side == 'LONG' else entry - 2.5 * atr

    algos, algo_status = await api.get_open_algo_orders(symbol)
    if algo_status != 200:
        state.reconcile_ready = False
        state.last_readiness_reason = 'Không đọc được Algo orders'
        return False
    hard_sl_orders = _matching_hard_sls(algos, side)
    hard_sl_order = next(
        (
            order for order in hard_sl_orders
            if str(order.get('clientAlgoId', '')).startswith('smc_')
        ),
        hard_sl_orders[0] if hard_sl_orders else None,
    )
    if hard_sl_order:
        position.hard_sl_algo_id = hard_sl_order.get('algoId')
        position.hard_sl_client_algo_id = hard_sl_order.get('clientAlgoId')
        # Dọn duplicate do một lần POST trước đây mất response, không đụng lệnh tay.
        for duplicate in hard_sl_orders:
            if (
                duplicate.get('algoId') != position.hard_sl_algo_id
                and str(duplicate.get('clientAlgoId', '')).startswith('smc_')
            ):
                await api.cancel_algo_order(duplicate.get('algoId'))
        levels_ready = all(
            value > 0 for value in (
                position.hard_sl, position.soft_sl, position.soft_tp1, position.soft_tp2
            )
        )
        state.reconcile_ready = levels_ready
        if not levels_ready:
            state.last_readiness_reason = 'Đã có Hard SL nhưng local SL/TP chưa phục hồi'
        if levels_ready and getattr(state, 'execution_unknown', False):
            _clear_unknown_execution(state, executed=True)
        return levels_ready

    if position.hard_sl <= 0:
        state.reconcile_ready = False
        state.last_readiness_reason = 'Vị thế chưa có Hard SL và ATR chưa sẵn sàng'
        return False

    result = await _create_recovery_hard_sl(state, api, symbol, side, position.hard_sl)
    if not result.get('algoId'):
        state.reconcile_ready = False
        state.last_readiness_reason = f'Không tái tạo được Hard SL: {result}'
        return False
    position.hard_sl_algo_id = result['algoId']
    position.hard_sl_client_algo_id = result.get('clientAlgoId')
    state.reconcile_ready = True
    if getattr(state, 'execution_unknown', False):
        _clear_unknown_execution(state, executed=True)
    logging.warning('🛡️ [RECONCILE] Đã tái tạo Hard SL algoId=%s', result['algoId'])
    return True


async def vong_lap_dong_bo(state):
    logging.info('💾 [ROM] Atomic backup đã khởi động.')
    while True:
        try:
            await asyncio.to_thread(_atomic_write, _snapshot(state.vi_the_hien_tai), ROM_PATH)
        except Exception as exc:
            logging.error('❌ [ROM] Lỗi ghi backup: %s', exc)
        await asyncio.sleep(2)


async def vong_lap_doi_chieu(state, api):
    logging.info('🔄 [RECONCILE] Đối chiếu vị thế/Algo orders đã khởi động.')
    while True:
        try:
            state.balance_usdt = await api.get_balance()
            await reconcile_once(state, api)
        except Exception as exc:
            state.reconcile_ready = False
            logging.exception('❌ [RECONCILE] Lỗi: %s', exc)
        await asyncio.sleep(5)
