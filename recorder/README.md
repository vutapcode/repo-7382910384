# SMC2026 Market Black Box

Recorder chạy độc lập với bot giao dịch và chỉ dùng public market data của
Binance USD-M Futures. Recorder không import module đặt lệnh, không đọc API key
và không thể gửi order.

## Luồng được ghi

- `agg_trade`
- `book_ticker` dựng đồng bộ từ local depth 100 ms
- `depth_snapshot`, `depth_diff`, `depth_checkpoint`
- `kline_1m`, `kline_15m`
- `mark_price`, `premium_index`, `open_interest`
- `liquidation`: force-liquidation thật từ `BTCUSDT@forceOrder`
- `bot_event`, `bot_cycles_snapshot`
- `feature_1s`: flow, spread, depth bands, OI/funding và liquidation theo giây
- `recorder_health_event`

WAL nằm ở `smc2026_data/raw/wal`; các giờ đã đóng được compact sang Parquet
ZSTD tại `smc2026_data/raw/parquet`. Recorder giữ tối đa 24 giờ raw theo
partition UTC và prune cả WAL lẫn Parquet lúc khởi động, sau đó mỗi 60 giây.
Có thể đổi bằng `SMC_RECORDER_RETENTION_HOURS`, nhưng giá trị phải lớn hơn 0.
Prune không chạm `derived/`, `health/`, `metadata/` hoặc ROM vị thế của bot.
Health hiện tại: `smc2026_data/health/status.json`.

Mỗi record V2 có `code_version` và `config_version`. Decision timeline ghi mọi
lần ARMED được đánh giá với kết quả `VETO`, `CORE_REJECT`, `CLAIMED`, sau đó nối
tiếp preflight, risk-size và economic fee gate.

## Replay

Replay dùng `receive_time_ms` làm đồng hồ ảo, merge các stream bằng k-way merge
để RAM không tăng theo kích thước lịch sử. Mặc định warm-up 90 giây để nạp depth
checkpoint gần nhất:

```bash
python3 -m recorder.replay \
  --start 2026-08-06T13:00:00Z \
  --end 2026-08-06T13:15:00Z
```

Điều tra riêng một setup:

```bash
python3 -m recorder.replay --start 1786021200000 \
  --setup-id 'BTCUSDT:TREND-PULLBACK:LONG:0:sv1:a1'
```

Replay là read-only, không import API đặt lệnh và không thể gửi order.

## Snapshot tài khoản Testnet cho Codex

Điểm vào cố định cho điều tra tài khoản là `recorder/account_access.json`.
File này ghi rõ Testnet URL, symbol, vị trí `.env`, tên hai biến credential và
lệnh refresh; raw API key/secret không được nhân bản vào source hoặc snapshot.

Tạo/cập nhật snapshot offline bằng signed `USER_DATA` GET endpoints:

```bash
python3 -m recorder.account_audit
```

Kết quả nằm tại `recorder/account_snapshot.json`, quyền file `0600`, gồm balance,
position, order/fill/income/algo gần nhất và round-trip đã nối với
`cycles.json`. CLI này chạy on-demand và tách khỏi daemon recorder public-only;
nó không gọi endpoint đặt, sửa hoặc hủy lệnh.

## Nguyên tắc an toàn

- Queue có giới hạn và mọi drop đều làm health chuyển `DEGRADED`.
- Depth `pu` không nối với `u` trước đó sẽ tăng gap counter và buộc resnapshot.
- Writer fsync từng batch; compactor ghi file tạm rồi atomic rename.
- Recorder hỏng không làm bot giao dịch dừng.
