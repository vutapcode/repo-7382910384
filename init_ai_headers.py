import os

# Cấu trúc và nội dung CONTEXT.md cho các thư mục chính
CONTEXT_FILES = {
    "1_tai_du_lieu": """# [AI CONTEXT] - KHỐI 1: TẢI DỮ LIỆU
    
> [!WARNING]
> - Nhiệm vụ: CHỈ chạy WebSocket/REST API để hứng data thị trường.
> - Data in: Binance (aggTrade, bookTicker, depth20, kline).
> - Data out: Ghi đè CỜ/DỮ LIỆU vào biến STATE trong `loi_he_thong/bo_nho_ram.py`.
> - CẤM: Không đặt logic phân tích, không tính toán ATR/RSI/BOS ở đây!
""",
    "2_suy_luan_mapping": """# [AI CONTEXT] - KHỐI 2: SUY LUẬN MAPPING
    
> [!IMPORTANT]
> - Nhiệm vụ: BỘ NÃO. Xử lý thuật toán, nhận diện mẫu (BOS, CHoCH, VAH/VAL), chấm điểm.
> - Data in: CHỈ ĐỌC TỪ `loi_he_thong/bo_nho_ram.py`.
> - Data out: Set các cờ ARMED, VETO, SCORE, ENTRY_SIDE vào RAM.
> - CẤM: Không gọi API mạng (I/O block), mọi thứ tính toán dựa trên RAM.
""",
    "3_thuc_thi": """# [AI CONTEXT] - KHỐI 3: THỰC THI (TRADE GUARDIAN)
    
> [!CAUTION]
> - Nhiệm vụ: Tốc độ < 1ms. Đọc tín hiệu và bắn lệnh KHẨN CẤP.
> - Data in: Đọc liên tục (orjson) từ `loi_he_thong/bo_nho_ram.py`.
> - Data out: Call POST/DELETE Binance API. Bắn asyncio Task Fire-and-Forget.
> - CẤM: Không ngâm logic phức tạp, không xử lý data mảng dài. IF chạm SL/TP -> FIRE.
""",
    "loi_he_thong": """# [AI CONTEXT] - LÕI HỆ THỐNG (BO NHO RAM)
    
> [!NOTE]
> - Nhiệm vụ: THE STATE MANAGER. Nơi duy nhất lưu trữ sinh mệnh của Bot.
> - Đặc tính: Dùng Lock (nếu cần) hoặc pure dict để chia sẻ RAM cho các Worker.
> - CẤM: Không chứa bất kỳ loop tải data hay logic toán học nào. File này phải nhẹ nhất có thể.
"""
}

# Cấu trúc file Python cần tạo kèm Header (dựa trên So_do.txt)
PYTHON_FILES_WITH_HEADERS = {
    "1_tai_du_lieu/tai_dong_tien/tai_dong_tien.py": {
        "module": "1_tai_du_lieu / tai_dong_tien",
        "role": "WebSocket: Hứng gói aggTrade (Khớp lệnh thực tế).",
        "io": "IN: Binance WS | OUT: RAM (bo_nho_ram)"
    },
    "1_tai_du_lieu/tai_gia_tick/tai_gia_tick.py": {
        "module": "1_tai_du_lieu / tai_gia_tick",
        "role": "WebSocket: Hứng giá @bookTicker liên tục (Cấp máu cho Vệ Sĩ).",
        "io": "IN: Binance WS | OUT: RAM"
    },
    "1_tai_du_lieu/tai_nen_live/tai_nen_live.py": {
        "module": "1_tai_du_lieu / tai_nen_live",
        "role": "WebSocket: Hứng nến kline 1m, 15m đang chạy chưa đóng.",
        "io": "IN: Binance WS | OUT: RAM"
    },
    "1_tai_du_lieu/tai_so_lenh/tai_so_lenh.py": {
        "module": "1_tai_du_lieu / tai_so_lenh",
        "role": "WebSocket: Hứng @depth20; giữ Top 10 cho execution estimate.",
        "io": "IN: Binance WS | OUT: RAM"
    },
    "1_tai_du_lieu/tai_nen_offline/tai_nen_offline.py": {
        "module": "1_tai_du_lieu / tai_nen_offline",
        "role": "REST API: Tải lịch sử nến M1, M15 (Chạy chậm định kỳ 15p).",
        "io": "IN: Binance REST | OUT: JSON / RAM"
    },
    "1_tai_du_lieu/tai_vi_mo/tai_vi_mo.py": {
        "module": "1_tai_du_lieu / tai_vi_mo",
        "role": "REST/WS: Quét Open Interest (OI) & Funding Rate.",
        "io": "IN: Binance API | OUT: RAM"
    },
    
    "2_suy_luan_mapping/map-nen-offline/ATR.py": {
        "module": "2_suy_luan_mapping / map-nen-offline",
        "role": "Tính toán Average True Range đo lường mức độ biến động.",
        "io": "IN: RAM (Nến) | OUT: RAM (Giá trị ATR)"
    },
    "2_suy_luan_mapping/map-nen-offline/BOS_CHoCH.py": {
        "module": "2_suy_luan_mapping / map-nen-offline",
        "role": "Quét Swing High/Low, xác định cấu trúc vỡ trend SMC.",
        "io": "IN: RAM (Nến) | OUT: RAM (Swing Points, Trend state)"
    },
    "2_suy_luan_mapping/map-nen-offline/POC_VAH_VAL.py": {
        "module": "2_suy_luan_mapping / map-nen-offline",
        "role": "Phân bổ Volume Profile tìm vùng cản cứng (Value Area).",
        "io": "IN: RAM (Nến/Vol) | OUT: RAM (VAH/VAL/POC)"
    },
    "2_suy_luan_mapping/map_dong_tien/delta_cvd.py": {
        "module": "2_suy_luan_mapping / map_dong_tien",
        "role": "Tính Delta Mua/Bán & cộng dồn tạo đường xu hướng CVD.",
        "io": "IN: RAM (aggTrade) | OUT: RAM (CVD Line)"
    },
    "2_suy_luan_mapping/map_dong_tien/flash_flow.py": {
        "module": "2_suy_luan_mapping / map_dong_tien",
        "role": "Radar gia tốc: Bật cờ đỏ khi dòng tiền bị nhồi/xả đột biến.",
        "io": "IN: RAM (aggTrade, CVD) | OUT: RAM (Flash Flag = True/False)"
    },
    "2_suy_luan_mapping/map_dong_tien/footprint.py": {
        "module": "2_suy_luan_mapping / map_dong_tien",
        "role": "Chẻ nhỏ nến, dò Imbalance tại từng tick giá siêu vi.",
        "io": "IN: RAM | OUT: RAM"
    },
    "2_suy_luan_mapping/tong_ket_chi_huy/chon_che_do.py": {
        "module": "2_suy_luan_mapping / tong_ket_chi_huy",
        "role": "MODE SELECT: Bắt mạch thị trường (Trending, Ranging, Squeeze).",
        "io": "IN: RAM | OUT: RAM (Current Mode)"
    },
    "2_suy_luan_mapping/tong_ket_chi_huy/kiem_duyet_veto.py": {
        "module": "2_suy_luan_mapping / tong_ket_chi_huy",
        "role": "VETO GATE: Cửa kiểm duyệt tử thần, chặn lệnh rủi ro cao.",
        "io": "IN: RAM (Flash Flag, OI) | OUT: RAM (VETO = True/False)"
    },
    "2_suy_luan_mapping/tong_ket_chi_huy/cham_diem.py": {
        "module": "2_suy_luan_mapping / tong_ket_chi_huy",
        "role": "SCORING: Đếm điểm hội lưu (Confluence) từ các chỉ báo (Core + Shark).",
        "io": "IN: RAM | OUT: RAM (Score/10)"
    },
    "2_suy_luan_mapping/tong_ket_chi_huy/chi_huy_truong.py": {
        "module": "2_suy_luan_mapping / tong_ket_chi_huy",
        "role": "HUB Giao tiếp: Tổng hợp điểm, chốt kịch bản và ra lệnh Bắn.",
        "io": "IN: RAM | OUT: RAM (Armed Signal)"
    },
    
    "3_thuc_thi/quan_ly_vi_the/tinh_toan_rui_ro.py": {
        "module": "3_thuc_thi / quan_ly_vi_the",
        "role": "Chạy trên RAM: Tính Size lệnh, Blended Entry, SL/TP cứng.",
        "io": "IN: RAM | OUT: RAM (Order Params)"
    },
    "3_thuc_thi/quan_ly_vi_the/dong_bo_trang_thai.py": {
        "module": "3_thuc_thi / quan_ly_vi_the",
        "role": "Chạy nền I/O: Ghi đè trạng thái sinh tử xuống ổ cứng (JSON Backup).",
        "io": "IN: RAM | OUT: File HDD"
    },
    "3_thuc_thi/ve_si_lenh/tho_san_trailing.py": {
        "module": "3_thuc_thi / ve_si_lenh",
        "role": "Tối ưu hóa lợi nhuận: Dời SL/TP linh hoạt theo EMA hoặc Tick.",
        "io": "IN: RAM | OUT: Bắn tín hiệu chốt"
    },
    "3_thuc_thi/ve_si_lenh/bao_ve_khan_cap.py": {
        "module": "3_thuc_thi / ve_si_lenh",
        "role": "Cầu dao điện: EJECT khẩn cấp khi ngửi thấy mùi khét (Tường bị rút).",
        "io": "IN: RAM (Wall Pull) | OUT: Lệnh thoát hiểm"
    },
    "3_thuc_thi/dat_lenh.py": {
        "module": "3_thuc_thi",
        "role": "I/O API: Cổng kết nối Binance (POST/DELETE lệnh thực tế).",
        "io": "IN: Signal | OUT: API Server"
    },
    "3_thuc_thi/giam_sat_he_thong.py": {
        "module": "3_thuc_thi",
        "role": "WATCHDOG: Quét Ping mạng liên tục, báo động khi nghẽn.",
        "io": "IN: System/Ping | OUT: Console/Log"
    },
    "loi_he_thong/bo_nho_ram.py": {
        "module": "loi_he_thong",
        "role": "Không gian lưu trữ siêu tốc (State). Nơi MỌI biến số tập trung.",
        "io": "IN/OUT: Shared Memory"
    }
}

HEADER_TEMPLATE = '''"""
[AI_CONTEXT]
- MODULE: {module}
- ROLE: {role}
- I/O: {io}
- RULE: CHỈ tuân thủ ranh giới của khối, không cắm chéo.
"""

'''

def create_context_files(base_path):
    print("Tạo các file CONTEXT.md cho các thư mục chính...")
    for folder, content in CONTEXT_FILES.items():
        folder_path = os.path.join(base_path, folder)
        os.makedirs(folder_path, exist_ok=True)
        file_path = os.path.join(folder_path, "CONTEXT.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content.strip())
        print(f" -> Đã tạo {file_path}")

def init_python_files(base_path):
    print("\\nKhởi tạo các file Python và nhúng AI Headers...")
    for rel_path, data in PYTHON_FILES_WITH_HEADERS.items():
        full_path = os.path.join(base_path, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        
        header_text = HEADER_TEMPLATE.format(
            module=data['module'],
            role=data['role'],
            io=data['io']
        )
        
        if not os.path.exists(full_path):
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(header_text)
            print(f" -> [NEW] Đã tạo và gắn Header: {rel_path}")
        else:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            if "[AI_CONTEXT]" not in content:
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(header_text + content)
                print(f" -> [UPDATED] Đã chèn Header vào file có sẵn: {rel_path}")
            else:
                print(f" -> [SKIP] Đã có Header: {rel_path}")

if __name__ == "__main__":
    BASE_DIR = r"C:\Users\Dang Gia Vu\Desktop\SMC2026"
    create_context_files(BASE_DIR)
    init_python_files(BASE_DIR)
    print("\\nHoàn tất việc thiết lập AI Context System!")
