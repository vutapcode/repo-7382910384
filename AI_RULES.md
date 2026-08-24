# SMC2026 - Bản Đồ Ngữ Cảnh Cho AI (AI Context Map)

> [!IMPORTANT]  
> Hỡi các AI Assistant (Cursor, Copilot, Gemini...), khi làm việc trong dự án này, BẠN PHẢI TUÂN THỦ TỐI ĐA kiến trúc 3 khối dưới đây. Dự án này là Bot Trading yêu cầu độ trễ (latency) nội bộ < 1ms, sử dụng `uvloop` và `asyncio`.

## 1. Nguyên Tắc Cốt Lõi (Core Rules)
- **Single Source of Truth**: Mọi dữ liệu realtime (giá, nến, sổ lệnh) đều được nạp vào RAM (`loi_he_thong/bo_nho_ram.py`). Các file khác CHỈ đọc từ RAM. Không truyền dữ liệu cồng kềnh qua lại giữa các hàm.
- **Tách biệt Data và Logic**: Khối 1 chỉ tải data bơm vào RAM. Khối 2 chỉ lấy data từ RAM ra tính toán. Khối 3 chỉ đọc RAM và đặt lệnh. CẤM CẮM CHÉO (Ví dụ: Không được viết logic vào lệnh ở khối 1).
- **Sub-ms Ping**: Khối Thực Thi (Khối 3) tuyệt đối KHÔNG có I/O block, KHÔNG query DB, KHÔNG gọi API (trừ khi bắn lệnh Market Close). Mọi thứ tính toán sẵn (SL/TP) phải nằm trên RAM.

## 2. Bản đồ Thư mục (Directory Map)
- `1_tai_du_lieu/`: Khối Input. Gồm các WebSocket Workers chạy song song. (Chỉ update RAM, không suy luận).
- `2_suy_luan_mapping/`: Khối Não. Đọc data từ RAM → Chạy các file phân tích (CVD, OI, VAH/VAL) → Đưa ra quyết định (ARMED/VETO/SCORE) → Cập nhật cờ vào RAM.
- `3_thuc_thi/`: Khối Bóp Cò. Async tasks chạy độc lập (`ve_si_lenh`). Quét RAM liên tục bằng `orjson`. Bắn lệnh thoát hiểm nhanh nhất có thể.
- `4_nghien_cuu_ai/`: Khối nghiên cứu bất đồng bộ. Chỉ đọc recorder/journal, tạo nhãn shadow và dataset; tuyệt đối không import Khối 3, không sửa RAM và không tham gia quyết định realtime.
- `loi_he_thong/`: Tim của hệ thống. Chứa biến State trung tâm.

## 3. Cách định vị khi code
- Bất cứ khi nào bạn mở 1 file `.py`, HÃY NHÌN VÀO DÒNG DOCSTRING ĐẦU TIÊN (AI Context Header) để biết mình đang ở lớp nào, đầu vào/đầu ra là gì. Không được tự ý import chéo phá vỡ kiến trúc khối.
