# Khối 4 - Nghiên cứu AI

Khối này chạy ngoài hot path của bot. Mọi worker chỉ được đọc dữ liệu recorder
và journal, rồi ghi derived dataset riêng. Không module nào trong đây được phép
import Commander, Executor, Binance account client hoặc thay đổi `SharedState`.

`gemini_shadow` phân tích hậu kiểm bằng Gemini 3.6 Flash. Kết quả chỉ là nhãn
nghiên cứu, không phải tín hiệu giao dịch.

## Gemini shadow worker

- Live cadence: một regime review cho mỗi bucket 15 phút đã ổn định 30 giây.
- Cycle cadence: review sau khi cycle đóng và đủ dữ liệu 15 phút phía sau.
- Output: `/home/ubuntu/smc2026_data/derived/ai_shadow/records/YYYY-MM-DD.jsonl`.
- Health: `/home/ubuntu/smc2026_data/derived/ai_shadow/health/YYYY-MM-DD.jsonl`.
- Replay không gọi API: `python3 -m gemini_shadow.main --dry-run --replay-limit 2`
  từ thư mục `4_nghien_cuu_ai`.

Credential không được đặt trong repo. Unit systemd chỉ chạy khi file `.env`
riêng `~/.config/smc2026/gemini-shadow.env` tồn tại và không rỗng; file phải có
mode `0600`. Sau khi provision key, chạy
`systemctl --user start smc2026-gemini-shadow.service`.

Mỗi record chứa `analysis_id`, `input_hash`, model/prompt version, liên kết
cycle/setup, source windows, regime, nguyên nhân, data-quality flags, giả thuyết
nghiên cứu, bằng chứng ủng hộ/phản bác, confidence và usage. Không có trường
action/order/veto.
