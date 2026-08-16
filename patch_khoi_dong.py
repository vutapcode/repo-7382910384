import sys
import re

file_path = '/home/ubuntu/SMC2026/khoi_dong.py'
with open(file_path, 'r') as f:
    content = f.read()

pattern = r'tasks_mang = \[.*?\]\s*if api\.testnet:\s*tasks_mang\.append\(asyncio\.create_task\(supervise\(\s*\'executionBookTicker\',\s*lambda: tai_gia_tick\.hung_gia_tick_execution\(\s*\"btcusdt\", state, testnet=True\s*\),\s*\)\)\)'

replacement = '''tasks_mang = [
        # --- SPOT MAINNET --- (Chiến lược / Structure / Signal)
        asyncio.create_task(supervise('bookTicker', lambda: tai_gia_tick.hung_gia_tick_futures("btcusdt", state))),
        asyncio.create_task(supervise('depth20', lambda: tai_so_lenh.hung_so_lenh_futures("btcusdt", state))),
        asyncio.create_task(supervise('kline', lambda: tai_nen_live.hung_nen_live_futures("btcusdt", state))),
        asyncio.create_task(supervise('aggTrade_spot', lambda: tai_dong_tien.hung_dong_tien_spot("btcusdt", state))),
        asyncio.create_task(supervise('coinbase_spot', lambda: tai_coinbase.hung_coinbase_spot("BTC-USD", state))),
        
        # --- FUTURES MAINNET --- (Volume / CVD / Dòng tiền)
        asyncio.create_task(supervise('aggTrade_futures', lambda: tai_dong_tien.hung_dong_tien_futures_real("btcusdt", state))),
        
        # --- FUTURES MAINNET EXECUTION --- (Shadow Trading Data Layer)
        asyncio.create_task(supervise('executionBookTicker', lambda: tai_gia_tick.hung_gia_tick_execution("btcusdt", state))),
        asyncio.create_task(supervise('executionDepth20', lambda: tai_so_lenh.hung_so_lenh_futures_execution("btcusdt", state))),
    ]'''

new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)

if new_content == content:
    print("Warning: Regex did not match!")
else:
    with open(file_path, 'w') as f:
        f.write(new_content)
    import py_compile
    py_compile.compile(file_path, doraise=True)
    print("Patched khoi_dong.py successfully.")
