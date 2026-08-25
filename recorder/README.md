# SMC2026 Market Black Box

Recorder chạy độc lập với bot giao dịch và chỉ dùng public market data của
Binance USD-M Futures, Binance Spot và Coinbase Spot. Recorder không import
module đặt lệnh, không đọc API key và không thể gửi order.

Recorder và dữ liệu/replay của nó **không có trading authority**. Authority chỉ
được lần từ `mainnet_tier_s_lean_launcher.py`; xem `STRATEGY_AUTHORITY.md`.

## Luồng được ghi

- `futures_trade_100ms` (thay `agg_trade` raw ở schema mới)
- `book_ticker` dựng và kiểm tra continuity từ partial depth 100 ms
- `depth_diff`, `depth_checkpoint`, `depth_snapshot`: chỉ còn để replay dữ liệu
  lịch sử; recorder mới không ghi wall/depth rows
- `mark_price`, `premium_index`, `open_interest`
- `liquidation`: force-liquidation thật từ `BTCUSDT@forceOrder`
- `bot_event`, `bot_cycles_snapshot`
- `decision_counterfactual`: kết quả giả định 5/15/30/60 giây; V5 tách
  economic miss theo causal episode khỏi diagnostic wave/persistent shadow
- `feature_1s`: flow, spread, depth bands, OI/funding và liquidation theo giây
- `recorder_health_event`
- `binance_spot_trade_100ms`, `binance_spot_ticker`
- `coinbase_spot_trade_100ms` (`coinbase_spot_ticker` cũ vẫn replay được)
- `wavefront_candidate`: cash proposer, causal lead quality, frozen Bias/OI and
  qualified/rejected reason; always `authority=false`
- `wavefront_virtual_entry`, `wavefront_virtual_exit`: mutually-exclusive
  maker/taker execution twins using the same Guardian V6 and hard-risk logic
- `residual_edge_report`: empirical net Guardian outcome with hierarchical LCB;
  heuristic `13/20/35 bps` labels have no authority here
- `liquidity_response`: replay-only executed depletion/refill observation;
  static wall/cancel never counts as executed flow

WAL nằm ở `smc2026_data/raw/wal`; các giờ đã đóng được compact sang Parquet
ZSTD tại `smc2026_data/raw/parquet`. Recorder giữ tối đa 24 giờ raw theo
partition UTC và prune cả WAL lẫn Parquet lúc khởi động, sau đó mỗi 60 giây.
Có thể đổi bằng `SMC_RECORDER_RETENTION_HOURS`, nhưng giá trị phải lớn hơn 0.
Service production giữ 84 giờ: đủ cửa sổ kiểm định 72 giờ và 12 giờ dự phòng
theo tốc độ ghi/ổ đĩa đã đo trên Lightsail hiện tại.
Prune không chạm `derived/`, `health/`, `metadata/` hoặc ROM vị thế của bot.
Health hiện tại: `smc2026_data/health/status.json`.

Cash trade được cộng dồn chính xác theo bucket receive-time 100 ms; Binance Spot
BBO lấy từ depth5 100 ms rồi lấy mẫu 250 ms. Kline truyền thống không còn nằm
trong hot path. Micro-batch giữ nguyên tổng buy/sell volume nhưng giảm số
row, fsync và chi phí replay so với ghi từng trade.

Futures partial depth top-20 ở 100 ms chỉ dựng BBO và kiểm tra continuity trong
RAM; wall/depth bands không được ghi. OI recorder được poll theo cấu hình riêng
và không đọc state chiến thuật của bot.

Mỗi record V2 có `code_version` và `config_version`. Với runtime Tier-S hiện
tại, recorder ưu tiên ba lớp bằng chứng:

1. `DECISION_EVALUATED` mang `TIER_S_DECISION_RECORD_V2`: cùng một `cycle_id`
   và `causal_episode_id` chứa input Bias/S1/S2/OI 60 giây/5 phút, nhóm
   cash/derivative, persistence 2 bucket, chase/lead-lag, regime,
   Spot-Perp/Coinbase/exchange-independence và output
   decision/mode/edge/cost/veto/miss taxonomy. Hard-veto taxonomy chỉ
   được ghi khi Council output `GO`; metadata trên một cycle `WAIT` không được
   giả thành hành vi veto.
2. `ENTRY`/`POSITION_STATE`/`EXIT` mang `TIER_S_SHADOW_EXECUTION_V1`:
   target/actual qty, filter status, fee/slippage model, hard SL, Guardian/Risk,
   best/floor R, PnL bps/R và holding time. `POSITION_STATE` chỉ ghi khi trạng
   thái đổi (tối đa khoảng 1 Hz) hoặc heartbeat 5 giây.
3. Miss có đúng một counterfactual canonical cho mỗi `causal_episode_id`, tại
   anchor mạnh nhất trước horizon đầu tiên; không coi các micro-cycle tương quan
   là mẫu độc lập. Outcome 5/15/30/60 giây gồm signed close, MFE, MAE và hard-SL
   giả định. Futures flow sequence gap làm outcome `valid=false`; recorder không
   nối outcome qua gap. `qualified_now` tách trạng thái hiện tại khỏi
   `qualified_ever` đã latch. `RISK_DAILY_LOCK` được ghi cho từng `GO` đã journal
   nhưng bị daily loss breaker chặn, nên không bị đếm nhầm thành execution miss.
   Cycle chưa có `causal_episode_id` được gộp O(1) thành `diagnostic_wave_id` và
   luôn có `economic_miss_eligible=false`; chuyển động sau đó chỉ nằm ở
   `diagnostic_move_screen_passed`. Candidate persistent-metaorder cũng có outcome
   riêng với `authority=false`, tuyệt đối không mở/veto/promote Entry.

Raw market data chỉ là nền để kiểm chứng ba lớp trên. Depth wall, indicator phụ
và debug không gắn `cycle_id` không được xem là bằng chứng quyết định.

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

`WAVEFRONT_SHADOW` chạy song song trong recorder khi
`WSTRADE_WAVEFRONT_SHADOW=1`. Nó dùng receive-order để cấm lookahead, event-time
chỉ để đo thứ tự causal, Futures không bao giờ làm proposer. Mỗi candidate hợp
lệ tạo `MAKER_TWIN` (trade-through thật, TTL 750 ms) và `TAKER_TWIN` (executable
BBO); hai nhánh không phải hai opportunity. Không có time-stop: vị thế ảo được
Guardian/Risk đóng, hoặc bị đánh dấu invalid khi feed gap/CPU safety làm mất
tính liên tục. `DEFENSIVE`/`SAFETY_ONLY` dừng evaluator trước các thành phần
recorder thiết yếu.

Replay tái dựng Wavefront từ raw records và trả thêm `wavefront`,
`wavefront_generated_records`, `liquidity_response`. Kết quả không được tự
promotion; báo cáo luôn chứa `manual_approval_required=true`.
Thống kê promotion được checkpoint atomically theo `code_version/config_version`
ở `derived/wavefront/`, nên gate 14 ngày không phụ thuộc retention raw 84 giờ.
Nhánh ảo đang mở khi recorder restart luôn được đóng `valid=false` với lý do
`RECORDER_RESTART_GAP`; không phục hồi hoặc nối causal state qua restart.

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
