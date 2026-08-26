"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_nen_offline
- ROLE: REST API: Tải lịch sử nến M1, M15 (Chạy chậm định kỳ 15p).
- I/O: IN: Binance REST | OUT: JSON / RAM
- RULE: CHỈ tuân thủ ranh giới của khối, không cắm chéo.
"""

import os
import json
import asyncio
import aiohttp
import logging
from pathlib import Path
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

SYMBOL = "BTCUSDT"
BINANCE_API = "https://api.binance.com/api/v3/klines"

async def fetch_binance_klines(symbol: str, interval: str, limit: int):
    """Gọi API Binance lấy dữ liệu nến (Có cơ chế Retry/Backoff) bằng aiohttp"""
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    
    backoff_times = [1, 2, 4]
    
    async with aiohttp.ClientSession() as session:
        for attempt in range(4): # Lần 0 (Chính) + 3 lần Retry
            try:
                async with session.get(BINANCE_API, params=params, timeout=10) as response:
                    if response.status == 429:
                        retry_after = int(response.headers.get("Retry-After", backoff_times[min(attempt, 2)]))
                        logging.warning(f"⚠️ Bị Binance chặn Rate Limit (429)! Đợi {retry_after}s rồi thử lại...")
                        await asyncio.sleep(retry_after)
                        continue
                        
                    response.raise_for_status()
                    return await response.json()
                    
            except Exception as e:
                if attempt < 3:
                    wait_time = backoff_times[attempt]
                    logging.warning(f"⚠️ Lỗi tải nến {interval} (Thử lại {attempt+1}/3 sau {wait_time}s): {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logging.error(f"❌ Lỗi mạng vĩnh viễn khi tải nến {interval} sau 3 lần thử: {e}")
                    return None

async def cap_nhat_nen(interval: str, max_limit: int, update_limit: int, is_update: bool = False):
    """
    Logic cập nhật:
    - Lần 1 (is_update=False): Tải full max_limit.
    - Lần 2+ (is_update=True): Chỉ tải update_limit nến mới nhất.
    """
    so_luong_tai = update_limit if is_update else max_limit
    
    logging.info(f"Đang tải {so_luong_tai} nến {interval} từ Binance REST (Không dùng ổ cứng)...")
    du_lieu_moi = await fetch_binance_klines(SYMBOL, interval, so_luong_tai)
    
    if not du_lieu_moi:
        return [] # Trả về mảng rỗng nếu lỗi mạng
        
    return du_lieu_moi

async def main():
    logging.info("--- BẮT ĐẦU CẬP NHẬT NẾN OFFLINE ---")
    await cap_nhat_nen(interval="1m", max_limit=500, update_limit=15)
    await cap_nhat_nen(interval="15m", max_limit=200, update_limit=2)
    logging.info("--- HOÀN TẤT ---")

if __name__ == "__main__":
    asyncio.run(main())