"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / map-nen-offline
- ROLE: Phân bổ Volume Profile tìm vùng cản cứng (Value Area).
- I/O: IN: RAM (Nến/Vol) | OUT: RAM (VAH/VAL/POC)
- RULE: CHỈ tuân thủ ranh giới của khối, không cắm chéo.
"""

def get_h(k): return float(k[2]) if isinstance(k, list) else float(k['h'])
def get_l(k): return float(k[3]) if isinstance(k, list) else float(k['l'])
def get_v(k): return float(k[5]) if isinstance(k, list) else float(k['v'])

def filter_klines_by_range(klines_m1, price_low, price_high, overlap_pct=0.5):
    """
    Lọc nến M1 còn lại những nến có ít nhất overlap_pct% nằm trong
    khoảng giá [price_low, price_high] của M15.

    Args:
        klines_m1   : list nến M1 (List of lists từ json).
        price_low   : Swing Low M15 (giới hạn dưới của range).
        price_high  : Swing High M15 (giới hạn trên của range).
        overlap_pct : tỉ lệ tối thiểu thân nến phải nằm trong range (mặc định 0.5).

    Returns:
        list nến M1 đã lọc.
    """
    if price_low >= price_high:
        return klines_m1  # Không lọc nếu range không hợp lệ

    result = []
    for k in klines_m1:
        h = get_h(k)
        l = get_l(k)
        candle_range = h - l
        if candle_range <= 0:
            continue

        overlap_low   = max(l, price_low)
        overlap_high  = min(h, price_high)
        overlap       = overlap_high - overlap_low

        if overlap / candle_range >= overlap_pct:
            result.append(k)

    return result if result else klines_m1  # Fallback: trả toàn bộ nếu lọc hết


def select_profile_klines(
    klines_m1, price_low, price_high, atr=0.0, recent_limit=120,
):
    """Không để Volume Profile mắc kẹt trong swing range đã breakout.

    Khi close mới nhất đã rời range cũ một khoảng có ý nghĩa, profile chuyển
    sang cửa sổ gần nhất. Trong range bình thường vẫn giữ bộ lọc cấu trúc cũ.
    """
    rows = list(klines_m1 or ())
    if not rows:
        return rows
    try:
        last = rows[-1]
        close = float(last[4] if isinstance(last, list) else last['c'])
        low = float(price_low)
        high = float(price_high)
        buffer = max(abs(float(atr or 0.0)), abs(close) * 0.0002)
    except (KeyError, IndexError, TypeError, ValueError):
        return rows[-max(1, int(recent_limit)):]
    if low < high and (close > high + buffer or close < low - buffer):
        return rows[-max(1, int(recent_limit)):]
    return filter_klines_by_range(rows, low, high)


def calculate_volume_profile(klines, num_bins=100, value_area_pct=0.70):
    """
    Tính Volume Profile (POC, VAH, VAL) từ một mảng nến (khuyến nghị M1).

    Thuật toán xấp xỉ tuyến tính: volume mỗi nến chia đều cho các bin
    mà khoảng [Low, High] của nó cắt qua — không đọc tick-by-tick.

    Args:
        klines         : list các nến.
        num_bins       : số bin chia theo trục giá (mặc định 100 cho M1).
        value_area_pct : tỉ lệ volume mục tiêu cho Value Area (mặc định 0.70).

    Returns:
        dict {'poc': float, 'vah': float, 'val': float}
    """
    if not klines:
        return {'poc': None, 'vah': None, 'val': None}

    # ---- Bước 1: Xác định Range ----
    max_high = get_h(klines[0])
    min_low  = get_l(klines[0])
    
    for k in klines:
        h = get_h(k)
        l = get_l(k)
        if h > max_high:
            max_high = h
        if l < min_low:
            min_low = l

    price_range = max_high - min_low
    if price_range <= 0:
        mid = (max_high + min_low) / 2.0
        return {'poc': mid, 'vah': mid, 'val': mid}

    bin_size     = price_range / num_bins
    bins         = [0.0] * num_bins
    last_bin_idx = num_bins - 1

    # ---- Bước 2: Phân bổ Volume vào Bin ----
    for k in klines:
        h = get_h(k)
        l = get_l(k)
        v = get_v(k)

        start_bin = int((l - min_low) / bin_size)
        end_bin   = int((h - min_low) / bin_size)

        start_bin = max(0, min(start_bin, last_bin_idx))
        end_bin   = max(0, min(end_bin,   last_bin_idx))

        if start_bin > end_bin:
            start_bin, end_bin = end_bin, start_bin

        span        = end_bin - start_bin + 1
        vol_per_bin = v / span

        for i in range(start_bin, end_bin + 1):
            bins[i] += vol_per_bin

    # ---- Bước 3: Xác định POC ----
    poc_idx = 0
    poc_vol = bins[0]
    for i in range(1, num_bins):
        if bins[i] > poc_vol:
            poc_vol = bins[i]
            poc_idx = i

    poc_price = min_low + (poc_idx + 0.5) * bin_size

    # ---- Bước 4: Xác định VAH / VAL ----
    total_volume  = sum(bins)
    target_volume = total_volume * value_area_pct

    accumulated = bins[poc_idx]
    lower_idx   = poc_idx
    upper_idx   = poc_idx

    while accumulated < target_volume:
        next_lower = lower_idx - 1
        next_upper = upper_idx + 1

        lower_vol = bins[next_lower] if next_lower >= 0            else -1.0
        upper_vol = bins[next_upper] if next_upper <= last_bin_idx else -1.0

        if lower_vol < 0 and upper_vol < 0:
            break

        if upper_vol >= lower_vol:
            accumulated += upper_vol
            upper_idx    = next_upper
        else:
            accumulated += lower_vol
            lower_idx    = next_lower

    val_price = min_low + lower_idx        * bin_size
    vah_price = min_low + (upper_idx + 1) * bin_size

    lvn_zones = find_lvn_zones(bins, min_low, bin_size)
    return {'poc': poc_price, 'vah': vah_price, 'val': val_price, 'lvn_zones': lvn_zones}



def find_lvn_zones(bins, min_low, bin_size, percentile_threshold=20):
    """
    Tìm các vùng LVN (Low Volume Node): vùng giá có volume thấp bất thường.
    Volume thấp => giá di chuyển nhanh, không nên Fade tại đây.
    Returns: list of dict {'price_low': float, 'price_high': float, 'avg_vol': float}
    """
    if not bins or len(bins) < 5:
        return []
    sorted_vols = sorted(bins)
    threshold = sorted_vols[max(0, int(len(sorted_vols) * percentile_threshold / 100))]
    lvn_zones = []
    in_lvn = False
    lvn_start = 0
    lvn_vols = []
    for i, vol in enumerate(bins):
        if vol <= threshold:
            if not in_lvn:
                in_lvn = True
                lvn_start = i
                lvn_vols = []
            lvn_vols.append(vol)
        else:
            if in_lvn:
                in_lvn = False
                lvn_zones.append({
                    'price_low': min_low + lvn_start * bin_size,
                    'price_high': min_low + i * bin_size,
                    'avg_vol': sum(lvn_vols) / max(len(lvn_vols), 1),
                })
    if in_lvn:
        lvn_zones.append({
            'price_low': min_low + lvn_start * bin_size,
            'price_high': min_low + len(bins) * bin_size,
            'avg_vol': sum(lvn_vols) / max(len(lvn_vols), 1),
        })
    return lvn_zones
