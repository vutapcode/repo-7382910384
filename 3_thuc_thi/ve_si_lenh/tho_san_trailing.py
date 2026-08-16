"""Smart trailing và một lần pyramid có kiểm soát."""

import asyncio
import logging
import math
import time
import importlib.util
from pathlib import Path

try:
    from loi_he_thong.order_identity import client_order_id as forensic_order_id
except ModuleNotFoundError:
    _identity_spec = importlib.util.spec_from_file_location(
        'trailing_order_identity',
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
    'shark_context',
    CURRENT_DIR.parents[1] / '2_suy_luan_mapping' / 'tong_ket_chi_huy' / 'shark_context.py',
)
journal_mod = _load_module(
    'nhat_ky_giao_dich_trailing',
    CURRENT_DIR.parent / 'quan_ly_vi_the' / 'nhat_ky_giao_dich.py',
)


def _floor_step(value, step):
    return round(math.floor((value + 1e-12) / step) * step, 12)


def _execution_prices(state):
    return (
        float(getattr(state, 'execution_best_bid', 0.0) or 0.0),
        float(getattr(state, 'execution_best_ask', 0.0) or 0.0),
    )


def _strategy_prices(state):
    return (
        float(getattr(state, 'best_bid', 0.0) or 0.0),
        float(getattr(state, 'best_ask', 0.0) or 0.0),
    )


def _veto_clear(state, side):
    wall = getattr(state, 'wall_pull_flag', {})
    if wall.get('active'):
        if side == 'LONG' and wall.get('side') == 'buy':
            return False
        if side == 'SHORT' and wall.get('side') == 'sell':
            return False
    threshold = max(
        getattr(state, 'p95_value', 3.0) * 3.0,
        getattr(state, 'vol_pct90', 0.0),
    )
    buy = float(getattr(state, 'current_cvd_buy_3s', 0.0) or 0.0)
    sell = float(getattr(state, 'current_cvd_sell_3s', 0.0) or 0.0)
    if side == 'LONG' and sell > threshold and sell > buy:
        return False
    if side == 'SHORT' and buy > threshold and buy > sell:
        return False
    return True


async def _try_add_on(state, api, shark=None):
    position = state.vi_the_hien_tai
    if getattr(state, 'execution_venue', '') == 'BINANCE_FUTURES_MAINNET':
        # Dedicated Mainnet account is capped at one 0.001 BTC position.
        position.add_on_attempted = True
        return
    if (
        position.add_on_attempted
        or position.tp1_done
        or not position.mode.startswith('TREND')
        or state.dang_xu_ly_dong_lenh
    ):
        return
    strategy_bid, strategy_ask = _strategy_prices(state)
    execution_bid, execution_ask = _execution_prices(state)
    current = strategy_ask if position.side == 'LONG' else strategy_bid
    execution_reference = execution_ask if position.side == 'LONG' else execution_bid
    if current <= 0.0:
        return
    atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
    profit = current - position.entry_price if position.side == 'LONG' else position.entry_price - current
    shark = shark or shark_mod.evaluate(state, position.side)
    shark_supports = (
        shark.get('status') == 'SHARK_SUPPORTIVE'
        and shark.get('support_count', 0) >= 2
        and shark.get('adverse_count', 0) == 0
    )
    if atr <= 0 or profit < atr or not _veto_clear(state, position.side) or not shark_supports:
        return

    position.add_on_attempted = True
    step = float(state.exchange_filters.get('step_size', 0.001))
    min_qty = float(state.exchange_filters.get('min_qty', step))
    add_qty = _floor_step(position.initial_qty * 0.25, step)
    min_notional = float(state.exchange_filters.get('min_notional', 0.0))
    required_notional_qty = (
        min_notional / execution_reference
        if min_notional and execution_reference > 0 else 0.0
    )
    if add_qty < max(min_qty, required_notional_qty):
        return

    state.dang_xu_ly_dong_lenh = True
    try:
        kwargs = {'positionSide': position.side} if state.account_hedge_mode else {}
        client_order_id = forensic_order_id(
            state, 'ADDON',
            opportunity_id=getattr(position, 'setup_semantic_key', None),
            setup_id=getattr(position, 'setup_id', None),
            generation=getattr(position, 'setup_generation', 0),
            nonce=int(time.time() * 1000),
        )
        result, status = await api.new_order(
            'BTCUSDT',
            'BUY' if position.side == 'LONG' else 'SELL',
            'MARKET',
            add_qty,
            newOrderRespType='RESULT',
            newClientOrderId=client_order_id,
            **kwargs,
        )
        if status == 599:
            await asyncio.sleep(0.5)
            result, status = await api.query_order('BTCUSDT', client_order_id)
        if status != 200:
            logging.warning("⚠️ [PYRAMID] Add-on thất bại: %s", result)
            return
        cycle_id = getattr(position, 'position_cycle_id', '')
        if cycle_id:
            journal_mod.record_actual_order(
                state, cycle_id, 'ADD_ON', result, add_qty, execution_reference,
                reason='SHARK_SUPPORTIVE',
                strategy_reference_price=current,
                execution_reference_price=execution_reference,
            )
        fill = float(result.get('avgPrice', 0.0) or execution_reference)
        old_qty = position.qty
        new_qty = old_qty + add_qty
        position.entry_price = (
            position.entry_price * old_qty + current * add_qty
        ) / new_qty
        position.strategy_entry_price = position.entry_price
        execution_entry = float(
            getattr(position, 'execution_entry_price', 0.0) or fill
        )
        position.execution_entry_price = (
            execution_entry * old_qty + fill * add_qty
        ) / new_qty
        position.venue_price_offset = (
            position.execution_entry_price - position.strategy_entry_price
        )
        position.qty = new_qty
        position.add_on_done = True
        # Sau add-on, bảo vệ ít nhất tại blended entry khi giá đã đi đủ 1 ATR.
        if position.side == 'LONG':
            position.soft_sl = max(position.soft_sl, position.entry_price)
        else:
            position.soft_sl = min(position.soft_sl, position.entry_price)
        logging.info(
            "➕ [PYRAMID] Thêm %.4f; Mainnet blended %.2f; "
            "Testnet blended %.2f; tổng %.4f",
            add_qty, position.entry_price, position.execution_entry_price, new_qty,
        )
    finally:
        state.dang_xu_ly_dong_lenh = False


async def vong_lap_trailing(state, api):
    logging.info(
        "🎯 [SMART TRAIL] EMA9 ± 0.25 ATR; %s.",
        (
            "MAINNET add-on bị khóa, tổng vị thế tối đa 0.001 BTC"
            if getattr(state, 'execution_venue', '') == 'BINANCE_FUTURES_MAINNET'
            else "pyramid tối đa một lần"
        ),
    )
    while True:
        try:
            if not state.co_lenh_mo or not state.vi_the_hien_tai.active:
                await asyncio.sleep(0.25)
                continue
            position = state.vi_the_hien_tai
            strategy_bid, strategy_ask = _strategy_prices(state)
            execution_bid, execution_ask = _execution_prices(state)
            if (
                strategy_bid <= 0.0 or strategy_ask <= strategy_bid
                or execution_bid <= 0.0 or execution_ask <= execution_bid
            ):
                await asyncio.sleep(0.25)
                continue
            shark = shark_mod.evaluate(state, position.side)
            await _try_add_on(state, api, shark)

            if position.trailing_active:
                ema9 = float(getattr(state, 'ema9_m1', 0.0) or 0.0)
                atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
                tick = float(state.exchange_filters.get('tick_size', 0.1))
                if ema9 > 0 and atr > 0:
                    status = shark.get('status', 'NEUTRAL')
                    buffer_mult = (
                        0.40 if status == 'SHARK_SUPPORTIVE'
                        else 0.10 if status == 'SHARK_ADVERSE'
                        else 0.25
                    )
                    if position.side == 'LONG':
                        candidate = ema9 - buffer_mult * atr
                        candidate = min(candidate, strategy_bid - tick)
                        position.soft_sl = max(position.soft_sl, candidate)
                    else:
                        candidate = ema9 + buffer_mult * atr
                        candidate = max(candidate, strategy_ask + tick)
                        position.soft_sl = min(position.soft_sl, candidate)
            await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("❌ [SMART TRAIL] Lỗi: %s", exc)
            await asyncio.sleep(1)
