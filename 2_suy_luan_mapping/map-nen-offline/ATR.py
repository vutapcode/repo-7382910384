"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / map-nen-offline
- ROLE: Tính toán Average True Range đo lường mức độ biến động.
- I/O: IN: RAM (Nến) | OUT: RAM (Giá trị ATR)
- RULE: CHỈ tuân thủ ranh giới của khối, không cắm chéo.
"""

def tinh_atr_1m(klines, period=14):
    """
    Tính Average True Range (ATR) cho chu kỳ period nến.
    
    Args:
        klines: list các nến dạng list/dict. Nếu format của Khối 1 lưu file json là:
                [open_time, open, high, low, close, volume, ...] thì index là:
                high=2, low=3, close=4.
        period: Số nến để tính trung bình (thường là 14).
        
    Returns:
        float: Giá trị ATR hiện tại. Nếu không đủ nến, trả về 0.0.
    """
    if len(klines) < period + 1:
        return 0.0
        
    true_ranges = []
    
    # Duyệt từ nến cũ đến mới để tính TR
    # Dùng nến gần nhất (last `period` nến) + 1 nến trước đó để lấy close_prev
    start_idx = len(klines) - period
    
    for i in range(start_idx, len(klines)):
        current_kline = klines[i]
        prev_kline = klines[i-1]
        
        # Check format, nếu là list từ Binance API
        if isinstance(current_kline, list):
            h = float(current_kline[2])
            l = float(current_kline[3])
            c_prev = float(prev_kline[4])
        # Nếu là dict (phòng trường hợp convert)
        elif isinstance(current_kline, dict):
            h = float(current_kline.get('h', 0))
            l = float(current_kline.get('l', 0))
            c_prev = float(prev_kline.get('c', 0))
        else:
            return 0.0

        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        true_ranges.append(tr)
        
    # Trung bình cộng đơn giản (SMA của TR) thay vì RMA để tính siêu tốc
    atr = sum(true_ranges) / len(true_ranges)
    return atr

