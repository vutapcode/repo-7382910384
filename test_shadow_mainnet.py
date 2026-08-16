import asyncio
import time
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

# Set env before importing
os.environ['SMC_EXECUTION_MODE'] = 'SHADOW_MAINNET'

class MockState:
    def __init__(self):
        self.hang_doi_tin_hieu = asyncio.Queue()
        self.execution_price_time = time.time()
        self.execution_depth_time = time.time()
        self.execution_best_bid = 100.0
        self.execution_best_ask = 101.0
        self.best_bid = 100.0
        self.best_ask = 101.0
        self.execution_bids_top_10 = [[100.0, 10.0]]
        self.execution_asks_top_10 = [[101.0, 10.0]]
        self.shadow_pending_orders = []
        self.shadow_positions = []

class TestShadowMainnet(unittest.IsolatedAsyncioTestCase):
    async def test_a_shadow_mainnet_opens_entry(self):
        import importlib
        dat_lenh_shadow = importlib.import_module('3_thuc_thi.dat_lenh_shadow')
        state = MockState()
        api = MagicMock()
        api.new_order = AsyncMock()
        api.new_algo_order = AsyncMock()
        
        signal = {'bias': 'LONG', 'quantity': 1.0, 'entry_style': 'MARKET', 'client_order_id': 'test1'}
        await state.hang_doi_tin_hieu.put(signal)
        
        task = asyncio.create_task(dat_lenh_shadow.vong_lap_shadow_thuc_thi(state, api))
        await asyncio.sleep(0.1)
        task.cancel()
        
        self.assertEqual(api.new_order.call_count, 0)
        self.assertEqual(api.new_algo_order.call_count, 0)
        self.assertEqual(len(state.shadow_positions), 1)

    async def test_b_stale_execution_bbo(self):
        import importlib
        dat_lenh_shadow = importlib.import_module('3_thuc_thi.dat_lenh_shadow')
        state = MockState()
        state.execution_price_time = time.time() - 5.0 # Stale > 3s
        api = MagicMock()
        
        signal = {'bias': 'LONG', 'quantity': 1.0, 'entry_style': 'MARKET'}
        await state.hang_doi_tin_hieu.put(signal)
        
        task = asyncio.create_task(dat_lenh_shadow.vong_lap_shadow_thuc_thi(state, api))
        await asyncio.sleep(0.1)
        task.cancel()
        
        self.assertEqual(len(state.shadow_positions), 0)

    async def test_e_passive_shadow_lifecycle(self):
        import importlib
        dat_lenh_shadow = importlib.import_module('3_thuc_thi.dat_lenh_shadow')
        state = MockState()
        state.execution_best_bid = 100.0
        state.execution_best_ask = 101.0
        api = MagicMock()
        
        signal = {'bias': 'LONG', 'quantity': 1.0, 'entry_style': 'PASSIVE_RETEST', 'entry_price': 99.0}
        await state.hang_doi_tin_hieu.put(signal)
        
        exec_task = asyncio.create_task(dat_lenh_shadow.vong_lap_shadow_thuc_thi(state, api))
        guard_task = asyncio.create_task(dat_lenh_shadow.vong_lap_shadow_guardian(state, api))
        
        await asyncio.sleep(0.2)
        # Should be pending because ask is 101.0 > 99.0
        self.assertEqual(len(state.shadow_pending_orders), 1)
        self.assertEqual(len(state.shadow_positions), 0)
        
        # Price drops
        state.execution_best_ask = 98.0
        state.execution_price_time = time.time()
        await asyncio.sleep(0.2)
        
        # Should be filled
        self.assertEqual(len(state.shadow_pending_orders), 0)
        self.assertEqual(len(state.shadow_positions), 1)
        
        exec_task.cancel()
        guard_task.cancel()

    async def test_f_shadow_sl_hit(self):
        import importlib
        dat_lenh_shadow = importlib.import_module('3_thuc_thi.dat_lenh_shadow')
        state = MockState()
        api = MagicMock()
        api.new_algo_order = AsyncMock()
        
        pos = {'id': 1, 'side': 'LONG', 'sl': 95.0, 'tp': 110.0}
        state.shadow_positions.append(pos)
        
        state.execution_best_bid = 94.0 # Hit SL
        state.execution_price_time = time.time()
        
        guard_task = asyncio.create_task(dat_lenh_shadow.vong_lap_shadow_guardian(state, api))
        await asyncio.sleep(0.2)
        guard_task.cancel()
        
        self.assertEqual(len(state.shadow_positions), 0) # Position closed in RAM
        self.assertEqual(api.new_algo_order.call_count, 0) # Zero Binance API write

if __name__ == '__main__':
    unittest.main()
