# [AI CONTEXT] - KHỐI 3: THỰC THI (TRADE GUARDIAN)
    
> [!CAUTION]
> - Nhiệm vụ: Tốc độ < 1ms. Đọc tín hiệu và bắn lệnh KHẨN CẤP.
> - Data in: Đọc liên tục (orjson) từ `loi_he_thong/bo_nho_ram.py`.
> - Data out: Call POST/DELETE Binance API. Bắn asyncio Task Fire-and-Forget.
> - CẤM: Không ngâm logic phức tạp, không xử lý data mảng dài. IF chạm SL/TP -> FIRE.