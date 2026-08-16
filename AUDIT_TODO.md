# Audit notes - TODO fix

## P0 / P1

1. **Critical worker crash có thể bị watchdog bật trading lại**
   - `khoi_dong.py` set `system_ready=False`, `trading_enabled=False` khi worker crash.
   - `3_thuc_thi/giam_sat_he_thong.py` có thể tự tính readiness rồi bật `trading_enabled` lại mà không biết critical task đang crash-loop.
   - Fix: health registry/heartbeat cho supervised tasks; readiness phải yêu cầu toàn bộ critical worker healthy; thêm warm-up sau restart và integration test kill worker.

2. **Tri-Oracle Tier-S VETO bị mất trên production snapshot path**
   - `kiem_duyet_veto.py` đọc `snapshot.tri_oracle_signal`.
   - `decision_snapshot.DECISION_FIELDS` không capture `tri_oracle_signal`.
   - Production Commander vì vậy fallback về `NEUTRAL`, làm divergence veto bị vô hiệu.
   - Fix: đưa signal + metadata cần thiết vào snapshot; test `SharedState -> capture -> Commander/VETO`.

3. **Momentum/tick reclaim confirmation gần như no-op**
   - `_has_momentum_reclaim()` dùng `prev_best_bid/prev_best_ask`.
   - Snapshot không capture hai field này.
   - Fallback dùng chính current BBO nên điều kiện direction gần như luôn pass.
   - Fix: snapshot previous BBO thật hoặc history; regression test giá quay ngược phải reject.

4. **Coinbase CVD có khả năng bị đảo dấu**
   - `tai_coinbase.py` đang hiểu `match.side` như taker side.
   - Cần xác minh semantics feed hiện tại; nếu `side` là maker side thì phải đảo dấu để convention luôn là `+ aggressive buy`, `- aggressive sell`.
   - Fix sau khi xác nhận protocol; thêm fixture test maker-buy/maker-sell.

5. **Tri-Oracle nói 3 nguồn xác nhận nhưng CONFIRMED chỉ kiểm tra 2 nguồn**
   - LONG/SHORT confirmed hiện chủ yếu yêu cầu Coinbase + Binance Spot.
   - Futures có thể ngược nhẹ mà vẫn bị gắn confirmed.
   - Fix: yêu cầu Futures cùng dấu hoặc tạo trạng thái partial/weak confirmation.

6. **Execution economics đang dùng sai venue**
   - `kinh_te_lenh.py` và một phần risk dùng `bids_top_10/asks_top_10`, `best_bid/ask` của signal/Spot book để ước lượng fill/slippage.
   - Lệnh thật chạy Binance Futures; repo đã có `execution_bids/asks` và execution BBO.
   - Fix: mọi fee/slippage/fill/risk gate phải dùng execution venue + freshness/venue tag; capture execution depth vào snapshot nếu cần.

7. **Công thức slippage trong risk model sai dimension**
   - `_slippage_estimate_bps()` có logic gần kiểu `ATR * 0.1 * price` để suy notional.
   - ATR đã là đơn vị giá; nhân thêm price làm sai đơn vị và dễ đẩy slippage lên cap.
   - Fix: dùng actual planned quantity * execution price rồi walk Futures book tính VWAP.

## P2 / hardening

8. **Daily safety gate chưa paginate history**
   - `mainnet_safety.refresh_daily_gate()` lấy tối đa 1000 orders/income records một lần.
   - Nếu vượt 1000 record trong ngày, entry count/PnL/fees có thể thiếu.
   - Fix: paginate đến đầu ngày/exhausted; nếu không chứng minh completeness thì fail-close.

9. **Data-feed hardening**
   - Coinbase CVD `sum(deque)` mỗi match có thể thành O(n) trên burst; dùng rolling accumulator.
   - `product_id` parameter nhưng subscribe đang hardcode BTC-USD.
   - CVD bucket dựa receive-time local; cân nhắc trade timestamp để tránh méo cửa sổ khi network stall.
   - Parse strict boolean cho `dualSidePosition` / `multiAssetsMargin`; tránh `bool("false") == True`.
   - Testnet execution depth đang lấy mainnet `fstream.binance.com`; cần tag/route rõ để economics test không hiểu nhầm venue.

## Những phần hiện chưa thấy cần đập lại

- `order_identity.py`
- runtime singleton lock
- unknown-order reconciliation
- query-by-client-ID trước retry
- orphan algo cleanup
- hard-SL restore
- tách `strategy_entry_price` / `execution_entry_price`
- guardian close idempotency
- mainnet fixed-quantity/budget/loss-streak protection

## Ghi chú audit

- Finding ban đầu về `dong_bo_trang_thai.py` thiếu sleep đã re-check và loại bỏ; loop vẫn sleep đúng.
- `dat_lenh.py` và `nhat_ky_giao_dich.py` quá lớn, GitHub content tool không trả trọn file trong một lần; cần audit tiếp theo chunk/đường gọi nếu muốn exhaustively từng dòng.

## Thứ tự sửa đề xuất

1. Health registry + snapshot wiring Tri-Oracle + reclaim.
2. Chuẩn hóa venue/CVD/slippage.
3. Pagination + feed/performance hardening.
