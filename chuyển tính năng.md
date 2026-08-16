# Kế hoạch chuyển ý tưởng từ SMCxVolumeProfile sang SMC2026

Dựa trên việc đọc các file `main.py` và `smc_volume_profile.py` ở dự án cũ (`SMCxVolumeProfile`), em nhận thấy dự án cũ đang có tình trạng **"Gộp chung I/O và Logic"** (ví dụ file `smc_volume_profile.py` vừa gọi `requests.get` để tải dữ liệu, vừa xử lý tính toán đỉnh đáy, Volume Profile). 

Để tương thích với kiến trúc 3 khối Siêu Tốc của SMC2026, em đề xuất kế hoạch "chẻ nhỏ" và di dời như sau:

## 1. Tách phần I/O (Tải Data) đẩy vào Khối 1
- **File cũ**: Hàm `fetch_klines()` trong `smc_volume_profile.py`.
- **Đích đến SMC2026**: `1_tai_du_lieu/tai_nen_offline/tai_nen_offline.py`.
- **Hành động**: Đưa đoạn request Binance REST API sang đây. Kết quả tải về sẽ không tính toán ngay, mà chỉ **ghi nến thẳng vào RAM** (`loi_he_thong/bo_nho_ram.py`) hoặc file JSON.
- **WebSocket của footprint.py**: Các vòng lặp kết nối WS (như `aggTrade`, `bookTicker`) trong file `footprint.py` cũ sẽ bị bóc tách và ném vào các Worker thuộc `1_tai_du_lieu/tai_dong_tien/` và `1_tai_du_lieu/tai_gia_tick/`.

## 2. Tách phần Toán & Logic đẩy vào Khối 2
Mọi thuật toán tính toán giờ đây sẽ ĐỌC DỮ LIỆU TỪ RAM (do Khối 1 vừa nạp vào), cấm gọi API.

- **BOS & CHoCH**: 
  - **Cũ**: Hàm `get_macro_structure()` (Tìm Swing High/Low, định vị trend BULL/BEAR/NEUTRAL).
  - **Đích đến**: `2_suy_luan_mapping/map-nen-offline/BOS_CHoCH.py`.
- **Volume Profile**:
  - **Cũ**: Hàm `filter_klines_by_range()` và `calculate_volume_profile()`.
  - **Đích đến**: `2_suy_luan_mapping/map-nen-offline/POC_VAH_VAL.py`.
- **Footprint Logic**:
  - Các thuật toán tính Imbalance, CVD, tính toán phân bổ lệnh theo tick (nếu có) từ `footprint.py` cũ sẽ dời vào thư mục `2_suy_luan_mapping/map_dong_tien/`.

## 3. Tách phần Quản lý Trạng Thái & Thực Thi đẩy vào Lõi và Khối 3
- **Biến State**: 
  - **Cũ**: Các dataclass `SniperState`, `FootprintState`, `DynamicThreshold` nằm trong `main.py` và `footprint.py`.
  - **Đích đến**: Tập trung tất cả vào `loi_he_thong/bo_nho_ram.py` để toàn bộ hệ thống cùng share RAM.
- **Orchestrator**:
  - Vòng lặp `main_loop()` chu kỳ 900s trong `main.py` sẽ trở thành hạt nhân của `khoi_dong.py` ở thư mục gốc SMC2026.
- **Vào Lệnh / Eject (Trade Guardian)**:
  - Logic bắt chạm SL, tính Market Close từ `footprint.py` sẽ chuyển thành các async task thuần túy trong `3_thuc_thi/ve_si_lenh/`.

## Lộ trình thực thi
1. **Bước 1**: Di dời `smc_volume_profile.py` trước (bóc tách I/O REST và các thuật toán tính đỉnh đáy/VP).
2. **Bước 2**: Chuyển đổi và nhúng các Class State vào `bo_nho_ram.py`.
3. **Bước 3**: Bóc tách các Worker WS và khối Thực thi từ file `footprint.py` vào các folder tương ứng.
4. **Bước 4**: Lắp ráp Orchestrator vào `khoi_dong.py`.
