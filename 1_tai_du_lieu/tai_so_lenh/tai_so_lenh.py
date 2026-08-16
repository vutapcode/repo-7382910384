"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_so_lenh
- ROLE: WebSocket: Hứng @depth20; giữ Top 10 riêng cho execution estimate.
- I/O: IN: Binance WS | OUT: RAM
- RULE: CHỈ tuân thủ ranh giới của khối, không cắm chéo.
"""

import asyncio
import orjson
import websockets
import logging
import time

async def hung_so_lenh_futures(symbol: str, bo_nho_ram):
    """
    Hàm kết nối WebSocket để hứng Sổ lệnh Top 20 từ Binance Futures.
    Tuyệt đối CHỈ thu thập danh sách Bids/Asks, không tính toán OBI hay Wall Pull ở đây.
    
    :param symbol: Cặp giao dịch Futures (Ví dụ: 'btcusdt')
    :param bo_nho_ram: Đối tượng RAM (Class TrangThai) để chứa mảng sổ lệnh
    """
    # Top 20 cho thấy wall nông so với thanh khoản rộng hơn; 100ms vẫn
    # nhẹ (20 level x 2 phía x 10Hz). Execution estimate chỉ dùng Top 10.
    stream_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@depth20@100ms"
    
    while True:
        try:
            async with websockets.connect(stream_url, ping_interval=20, ping_timeout=10) as ws:
                logging.info(f"📚 [ORDERBOOK] Đã kết nối luồng sổ lệnh Top 20: {symbol.upper()}")
                
                async for message in ws:
                    # Sử dụng orjson để giải mã siêu tốc độ
                    try:
                        data = orjson.loads(message)
                        
                        # Cấu trúc Binance trả về: 
                        # 'b': [ [Giá, Khối lượng], ... ] (Bids)
                        # 'a': [ [Giá, Khối lượng], ... ] (Asks)
                        
                        bids = [[float(price), float(qty)] for price, qty in data.get('b', [])]
                        asks = [[float(price), float(qty)] for price, qty in data.get('a', [])]
                        
                        # Partial-depth không có event-time phù hợp cho correlation;
                        # dùng reception-time đồng bộ với pipeline hiện tại.
                        # Sử dụng time.time() chuẩn (giây) thay vì ms để đồng bộ toàn hệ thống
                        current_ts = time.time()
                    except (KeyError, TypeError, ValueError):
                        continue
                        
                    # Đóng gói dữ liệu để ném vào Queue cho Khối 2 xử lý
                    snapshot = {'bids': bids, 'asks': asks, 'timestamp': current_ts}
                    
                    # Bơm trực tiếp danh sách vào RAM (Ghi đè để theo dõi nhanh)
                    bo_nho_ram.bids_top_10 = bids[:10]
                    bo_nho_ram.asks_top_10 = asks[:10]
                    
                    # Đẩy vào Hàng đợi (Queue) để Khối 2 đọc chậm không bị mất luồng (Backpressure)
                    bo_nho_ram.hang_doi_so_lenh.append(snapshot)
                    
                    # Cập nhật nhịp tim để giam_sat_he_thong.py (Watchdog) biết luồng vẫn sống
                    bo_nho_ram.thoi_gian_so_lenh_cuoi = snapshot['timestamp']


        except websockets.exceptions.ConnectionClosed:
            logging.warning("⚠️ [ORDERBOOK] Mất kết nối mạng. Đang kết nối lại...")
            await asyncio.sleep(1)
            
        except Exception as e:
            logging.error(f"❌ [ORDERBOOK] Lỗi ngoại lệ: {e}. Thử lại sau 2s...")
            await asyncio.sleep(2)

async def hung_so_lenh_futures_execution(symbol: str, state):
    """
    Hàm kết nối WebSocket để hứng Sổ lệnh Top 20 từ Binance Futures Mainnet phục vụ execution.
    """
    stream_url = f"wss://fstream.binance.com/ws/{symbol.lower()}@depth20@100ms"
    
    while True:
        try:
            async with websockets.connect(stream_url, ping_interval=20, ping_timeout=10) as ws:
                logging.info(f"📚 [ORDERBOOK-EXEC] Đã kết nối luồng Futures Mainnet: {symbol.upper()}")
                
                async for message in ws:
                    try:
                        data = orjson.loads(message)
                        
                        bids = [[float(price), float(qty)] for price, qty in data.get('b', [])]
                        asks = [[float(price), float(qty)] for price, qty in data.get('a', [])]
                        
                        state.execution_bids = bids
                        state.execution_asks = asks
                        state.execution_bids_top_10 = bids[:10]
                        state.execution_asks_top_10 = asks[:10]
                        
                    except (KeyError, TypeError, ValueError):
                        continue

        except websockets.exceptions.ConnectionClosed:
            logging.warning("⚠️ [ORDERBOOK-EXEC] Mất kết nối mạng. Đang kết nối lại...")
            await asyncio.sleep(1)
            
        except Exception as e:
            logging.error(f"❌ [ORDERBOOK-EXEC] Lỗi ngoại lệ: {e}. Thử lại sau 2s...")
            await asyncio.sleep(2)
