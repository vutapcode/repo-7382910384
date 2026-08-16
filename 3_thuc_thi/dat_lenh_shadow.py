import asyncio
import time
import logging
from pprint import pformat
from . import dat_lenh

import importlib.util
from pathlib import Path

def load_module(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

CURRENT_DIR = Path(__file__).resolve().parent
economic_mod = load_module(
    "kinh_te_lenh",
    CURRENT_DIR.parent / "2_suy_luan_mapping" / "tong_ket_chi_huy" / "kinh_te_lenh.py",
)

logger = logging.getLogger(__name__)

async def vong_lap_shadow_thuc_thi(state, api=None):
    """
    Tiêu thụ hang_doi_tin_hieu nhưng KHÔNG GỬI API.
    Giả lập fill, đẩy vào RAM để shadow_guardian quản lý.
    """
    logger.info("[SHADOW MAINNET] Khởi động Vòng lặp Thực thi Ảo (ZERO-WRITE API)")
    
    if not hasattr(state, 'shadow_pending_orders'):
        state.shadow_pending_orders = []
    if not hasattr(state, 'shadow_positions'):
        state.shadow_positions = []
        
    while True:
        try:
            signal = await state.hang_doi_tin_hieu.get()
            
            # Fail-closed check
            if not dat_lenh.require_fresh_execution_bbo(state):
                logger.warning("[SHADOW MAINNET] Từ chối lệnh do Execution BBO Stale/Zero!")
                continue
                
            qty = float(signal.get('quantity', 0.0))
            side = signal.get('bias')
            entry_style = str(signal.get('entry_style') or '').upper()
            
            if entry_style == 'PASSIVE_RETEST':
                # Tính giá limit
                strategy_ref = float(state.best_bid) if side == 'BUY' else float(state.best_ask)
                live = float(getattr(state, 'execution_best_bid' if side == 'BUY' else 'execution_best_ask', 0.0) or 0.0)
                requested = float(signal.get('entry_price', live))
                translated = requested + (live - strategy_ref)
                desired = min(translated, live) if side == 'BUY' else max(translated, live)
                
                order = {
                    'id': int(time.time() * 1000),
                    'clientOrderId': signal.get('client_order_id'),
                    'side': side,
                    'qty': qty,
                    'price': desired,
                    'status': 'PENDING',
                    'signal': signal,
                    'created_at': time.time()
                }
                state.shadow_pending_orders.append(order)
                logger.info(f"[SHADOW MAINNET] Tạo Lệnh chờ Ảo {side} Limit: {desired}")
            else:
                # Market fill
                entry_levels = state.execution_asks_top_10 if side == 'LONG' or side == 'BUY' else state.execution_bids_top_10
                fill = economic_mod.estimate_market_fill(entry_levels, qty)
                entry_price = float(fill.get('avg_price', 0.0))
                
                if entry_price > 0:
                    position = {
                        'id': int(time.time() * 1000),
                        'clientOrderId': signal.get('client_order_id'),
                        'side': side,
                        'qty': qty,
                        'entryPrice': entry_price,
                        'signal': signal,
                        'sl': signal.get('hard_sl') or signal.get('soft_sl'), # Sẽ được tính lại trong guardian nếu cần
                        'tp': signal.get('soft_tp1') or signal.get('hard_tp'),
                        'created_at': time.time()
                    }
                    state.shadow_positions.append(position)
                    logger.info(f"[SHADOW MAINNET] Khớp Market Ảo {side} tại giá {entry_price}")
        except Exception as e:
            logger.error(f"[SHADOW MAINNET] Lỗi thực thi ảo: {e}")
            await asyncio.sleep(1.0)


async def vong_lap_shadow_guardian(state, api=None):
    """
    Giám sát lệnh Pending (Khớp) và Position (SL/TP) dựa trên execution_bbo.
    Tuyệt đối không gọi Binance API.
    """
    logger.info("[SHADOW MAINNET] Khởi động Guardian Ảo (ZERO-WRITE API)")
    while True:
        try:
            await asyncio.sleep(0.1) # 100ms
            
            if not dat_lenh.require_fresh_execution_bbo(state):
                continue
                
            bid = getattr(state, 'execution_best_bid', 0.0)
            ask = getattr(state, 'execution_best_ask', 0.0)
            now = time.time()
            
            # Xử lý lệnh PENDING
            pending_orders = getattr(state, 'shadow_pending_orders', [])
            surviving_pending = []
            for order in pending_orders:
                ttl = order['signal'].get('passive_intent_ttl_seconds', 30.0)
                if now - order['created_at'] > ttl:
                    logger.info(f"[SHADOW MAINNET] Lệnh {order['id']} EXPIRED")
                    continue
                    
                filled = False
                if order['side'] == 'BUY' and ask <= order['price']:
                    filled = True
                elif (order['side'] == 'SHORT' or order['side'] == 'SELL') and bid >= order['price']:
                    filled = True
                    
                if filled:
                    order['status'] = 'FILLED'
                    order['entryPrice'] = order['price']
                    state.shadow_positions.append(order)
                    logger.info(f"[SHADOW MAINNET] Lệnh {order['id']} FILLED tại {order['price']}")
                else:
                    surviving_pending.append(order)
            state.shadow_pending_orders = surviving_pending
            
            # Xử lý POSITIONS (SL/TP)
            positions = getattr(state, 'shadow_positions', [])
            surviving_positions = []
            for pos in positions:
                closed = False
                close_reason = None
                close_price = 0.0
                
                # Tính toán lại SL/TP từ signal levels nếu cần, nhưng giả định đơn giản lấy từ pos
                sl = float(pos.get('sl', 0.0))
                tp = float(pos.get('tp', 0.0))
                
                if sl > 0.0 and tp > 0.0:
                    if pos['side'] == 'BUY' or pos['side'] == 'LONG':
                        if bid <= sl:
                            closed = True
                            close_reason = 'SHADOW_SL_HIT'
                            close_price = bid
                        elif bid >= tp:
                            closed = True
                            close_reason = 'SHADOW_TP_HIT'
                            close_price = bid
                    else:
                        if ask >= sl:
                            closed = True
                            close_reason = 'SHADOW_SL_HIT'
                            close_price = ask
                        elif ask <= tp:
                            closed = True
                            close_reason = 'SHADOW_TP_HIT'
                            close_price = ask
                            
                if closed:
                    logger.info(f"[SHADOW MAINNET] Vị thế {pos['id']} đóng do {close_reason} tại {close_price}")
                    # Thực tế nên ghi journal ở đây, ta in log tạm
                else:
                    surviving_positions.append(pos)
            state.shadow_positions = surviving_positions

        except Exception as e:
            logger.error(f"[SHADOW MAINNET] Lỗi guardian ảo: {e}")
            await asyncio.sleep(1.0)
