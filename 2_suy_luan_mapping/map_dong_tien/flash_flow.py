"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / map_dong_tien
- ROLE: Đếm và nhận diện lệnh thị trường cực lớn dựa trên Dynamic Threshold (Thuật toán Histogram).
- I/O: IN: RAM (Lệnh khớp) | OUT: RAM (Nguong_ca_map)
- RULE: CHỈ tuân thủ ranh giới của khối, không cắm chéo.
"""

import logging

def cap_nhat_nguong_ca_map(lenh_khop: dict, state):
    """
    Thuật toán Xấp xỉ Histogram + Sliding Window để tìm Percentile 95.
    Giảm độ phức tạp từ O(N log N) của sorted() xuống O(K) với K=500 Bins.
    """
    ts = lenh_khop.get('thoi_gian_ms', lenh_khop.get('thoi_gian', 0))
    # Lấy current_price từ best_bid (nếu chưa có thì lấy tạm giá khớp hiện tại)
    current_price = getattr(state, 'best_bid', 0.0)
    if current_price == 0.0:
        current_price = lenh_khop['gia']
        
    if ts == 0:
        return
        
    qty = lenh_khop['khoi_luong']
    
    # 1. Chèn vào Bin
    BIN_SIZE = 0.1
    MAX_BIN_IDX = 500 # Lệnh max 50 BTC
    
    state.dt_total_count += 1
    
    bin_idx = int(qty / BIN_SIZE)
    if bin_idx >= MAX_BIN_IDX:
        bin_idx = MAX_BIN_IDX
        state.dt_overflow_count += 1
        
        # Log overflow
        ty_le = (state.dt_overflow_count / state.dt_total_count) * 100
        logging.info(f"🐳 [Flash Flow] Lệnh {qty} BTC rơi vào giỏ tràn (Overflow). Tỷ lệ tràn: {ty_le:.3f}% ({state.dt_overflow_count}/{state.dt_total_count})")
        
    state.dt_deque.append((ts, bin_idx))
    state.dt_histogram[bin_idx] += 1
    
    # 2. Xóa các lệnh quá 30 phút (1800000 ms)
    cutoff = ts - 1800000
    while state.dt_deque and state.dt_deque[0][0] < cutoff:
        old_ts, old_bin = state.dt_deque.popleft()
        if state.dt_histogram[old_bin] > 0:
            state.dt_histogram[old_bin] -= 1
            
    # 3. Tìm P95 (Chỉ tính toán tối đa 1 lần/giây để tiết kiệm CPU)
    last_p95_ts = getattr(state, 'last_p95_ts', 0)
    if ts - last_p95_ts < 1000:
        return # Chưa đủ 1 giây, giữ nguyên P95 cũ
        
    state.last_p95_ts = ts
    
    total_samples = len(state.dt_deque)
    if total_samples == 0:
        return
        
    target = total_samples * 0.95
    accum = 0
    p95_bin = 0
    
    for i in range(MAX_BIN_IDX + 1):
        accum += state.dt_histogram[i]
        if accum >= target:
            p95_bin = i
            break
            
    p95_value = p95_bin * BIN_SIZE
    
    # 4. Tính USD floor (Tối thiểu 150,000 USD)
    usd_floor = 150000.0 / current_price
    
    # Lưu giá trị cuối cùng vào RAM (Max giữa P95 và USD Floor)
    state.p95_value = max(p95_value, usd_floor)
