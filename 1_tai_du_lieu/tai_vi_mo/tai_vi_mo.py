"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_vi_mo
- ROLE: REST/WS: Quét Open Interest (OI) & Funding Rate.
- I/O: IN: Binance API | OUT: RAM
- RULE: CHỈ tuân thủ ranh giới của khối, không cắm chéo.
"""

import asyncio
import orjson
import aiohttp
import logging
import time
from pathlib import Path

# Cấu hình đường dẫn lưu ROM tương đối so với thư mục gốc của dự án
THU_MUC_LUU_TRU = Path("1_tai_du_lieu/tai_vi_mo/data_luu_tru")
FILE_ROM = THU_MUC_LUU_TRU / "vi_mo.json"

async def tai_du_lieu_vi_mo(symbol: str, bo_nho_ram, chu_ky_giay: int = 5):
    """
    Hàm gọi REST API định kỳ để lấy Open Interest (OI) và Funding Rate từ Binance Futures.
    Sau đó bơm vào RAM và lưu một bản cứng (ROM) vào thư mục data_luu_tru.
    
    :param symbol: Cặp giao dịch Futures (Ví dụ: 'btcusdt')
    :param bo_nho_ram: Đối tượng RAM (Class TrangThai)
    :param chu_ky_giay: Thời gian nghỉ giữa các lần tải (Mặc định 5 giây để tránh bị sàn khoá IP)
    """
    # Endpoint REST của Binance USD-M Futures
    # Bỏ yêu cầu tạo thư mục ROM vì không dùng File I/O nữa
    
    symbol_upper = symbol.upper()
    
    # Endpoint REST của Binance USD-M Futures
    url_oi = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol_upper}"
    url_funding = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol_upper}"
    
    # Mở 1 session duy trì kết nối để gọi API nhanh hơn
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # 1. Tải Open Interest (Tổng khối lượng vị thế đang mở trên toàn sàn)
                async with session.get(url_oi, timeout=aiohttp.ClientTimeout(total=10)) as res_oi:
                    data_oi = await res_oi.json()
                    oi_hien_tai = float(data_oi['openInterest'])
                    
                # 2. Tải Funding Rate (Phí qua đêm / Cán cân Long/Short)
                async with session.get(url_funding, timeout=aiohttp.ClientTimeout(total=10)) as res_funding:
                    data_funding = await res_funding.json()
                    funding_hien_tai = float(data_funding['lastFundingRate'])
                
                # 3. BƠM VÀO RAM CHO KHỐI 2 (map_vi_mo.py)
                bo_nho_ram.open_interest = oi_hien_tai
                bo_nho_ram.funding_rate = funding_hien_tai
                bo_nho_ram.thoi_gian_vi_mo_cuoi = time.time()
                
                # 4. GHI RA ROM (LƯU BÊN CẠNH)
                # [DELETED] Đã xóa logic ghi File theo yêu cầu để không block/chống lãng phí I/O.
                # Chỉ nạp thẳng RAM là đủ.
                
                logging.debug(f"🌍 [VĨ MÔ] Cập nhật thành công -> OI: {oi_hien_tai} | Funding: {funding_hien_tai}")
                
                # Chạy chậm: Ngủ X giây theo chu kỳ trước khi fetch lại
                await asyncio.sleep(chu_ky_giay)
                
            except aiohttp.ClientError as e:
                logging.warning(f"⚠️ [VĨ MÔ] Lỗi mạng khi gọi REST API: {e}. Thử lại sau 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logging.error(f"❌ [VĨ MÔ] Lỗi ngoại lệ: {e}. Thử lại sau 10s...")
                await asyncio.sleep(10)