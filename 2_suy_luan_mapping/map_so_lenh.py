"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping
- ROLE: Tính OBI (Orderbook Imbalance) và nhận diện Wall Pull (Spoof) / Absorption.
- I/O: IN: RAM (Snapshot sổ lệnh) | OUT: RAM (OBI, cờ Wall Pull)
- RULE: Thuần toán học, không gọi I/O mạng.
"""
import logging
from collections import deque


ABSORPTION_CONFIRM_MIN_SECONDS = 0.5
ABSORPTION_CONFIRM_MAX_SECONDS = 1.0
TRADE_EVENT_LOOKBACK_SECONDS = 0.10
WALL_ADVERSE_FLOW_SHARE_MIN = 0.65
WALL_PRICE_DISPLACEMENT_MIN_ATR = 0.05
WALL_PRICE_DISPLACEMENT_MIN_BPS = 0.25
WALL_NET_FLOW_MIN_QTY = 0.10


def _book_mid(bids, asks, state=None):
    """Lấy mid từ chính depth snapshot; bookTicker chỉ là fallback."""
    try:
        bid = float(bids[0][0])
        ask = float(asks[0][0])
    except (IndexError, TypeError, ValueError):
        bid = float(getattr(state, 'best_bid', 0.0) or 0.0)
        ask = float(getattr(state, 'best_ask', 0.0) or 0.0)
    return (bid + ask) / 2.0 if bid > 0.0 and ask >= bid else 0.0


def _matched_flow(state, pull, current_time_s):
    """Trả cả hai phía aggTrade trong cùng một cửa sổ event-time.

    Wall-pull cần biết flow đối nghịch có thật sự chiếm ưu thế hay không;
    chỉ biết một phía khớp bao nhiêu là chưa đủ để hard-VETO.
    """
    start = float(pull.get('window_start_s', pull['time_s']))
    timeline = getattr(state, 'trade_flow_timeline', ())
    buy = sell = 0.0
    found_window_event = False
    for item in reversed(timeline):
        event_ts = float(item.get('ts', 0.0))
        if event_ts > current_time_s:
            continue
        if event_ts < start:
            break
        found_window_event = True
        buy += float(item.get('buy', 0.0) or 0.0)
        sell += float(item.get('sell', 0.0) or 0.0)
    if found_window_event:
        return buy, sell

    current_buy = float(getattr(state, 'cvd_buy', 0.0) or 0.0)
    current_sell = float(getattr(state, 'cvd_sell', 0.0) or 0.0)
    buy_start = pull.get('cvd_buy_start')
    sell_start = pull.get('cvd_sell_start')
    # Giữ tương thích với pending/test cũ chỉ có cvd_start của phía aggressor.
    if buy_start is None:
        buy_start = (
            pull.get('cvd_start', current_buy)
            if pull.get('side') == 'sell' else current_buy
        )
    if sell_start is None:
        sell_start = (
            pull.get('cvd_start', current_sell)
            if pull.get('side') == 'buy' else current_sell
        )
    return (
        max(0.0, current_buy - float(buy_start)),
        max(0.0, current_sell - float(sell_start)),
    )


def _matched_volume(state, pull, side, current_time_s):
    """Cộng aggTrade theo event-time; fallback CVD giữ tương thích warm-up/test."""
    buy, sell = _matched_flow(state, pull, current_time_s)
    return sell if side == 'buy' else buy


def _wall_confirmation(state, pull, current_mid, buy_flow, sell_flow):
    """Xác nhận độc lập trước khi wall-pull được phép hard-VETO.

    Depth disappearance vẫn được xuất bản như advisory. Quyền VETO chỉ được
    cấp khi giá đã follow-through bất lợi và aggTrade cùng hướng chiếm ưu thế.
    """
    side = pull.get('side')
    reference = float(pull.get('reference_price', 0.0) or 0.0)
    atr = float(
        pull.get('atr_at_pull', 0.0)
        or getattr(state, 'atr_1m', 0.0)
        or 0.0
    )
    if side == 'buy':
        adverse_flow, supporting_flow = sell_flow, buy_flow
        adverse_price_move = reference - current_mid
    else:
        adverse_flow, supporting_flow = buy_flow, sell_flow
        adverse_price_move = current_mid - reference

    total_flow = adverse_flow + supporting_flow
    adverse_share = adverse_flow / total_flow if total_flow > 0.0 else 0.0
    net_adverse = adverse_flow - supporting_flow
    drop = float(pull.get('drop', 0.0) or 0.0)
    p95 = float(getattr(state, 'p95_value', 0.0) or 0.0)
    # Không đòi flow bằng cả wall (vì wall-pull vốn là cancel), nhưng chặn một
    # vài lệnh bụi được dùng làm "corroboration" giả.
    flow_floor = max(
        WALL_NET_FLOW_MIN_QTY,
        min(0.10 * drop, max(WALL_NET_FLOW_MIN_QTY, 0.25 * p95)),
    )
    displacement_bps = (
        adverse_price_move / reference * 10000.0 if reference > 0.0 else 0.0
    )
    displacement_atr = adverse_price_move / atr if atr > 0.0 else 0.0
    price_confirmed = bool(
        reference > 0.0
        and current_mid > 0.0
        and displacement_bps >= WALL_PRICE_DISPLACEMENT_MIN_BPS
        and (
            atr <= 0.0
            or displacement_atr >= WALL_PRICE_DISPLACEMENT_MIN_ATR
        )
    )
    flow_corroborated = bool(
        adverse_share >= WALL_ADVERSE_FLOW_SHARE_MIN
        and net_adverse >= flow_floor
    )
    return {
        'reference_price': reference,
        'confirmation_price': float(current_mid or 0.0),
        'price_displacement_bps': displacement_bps,
        'price_displacement_atr': displacement_atr,
        'adverse_flow_qty': adverse_flow,
        'supporting_flow_qty': supporting_flow,
        'adverse_flow_share': adverse_share,
        'net_adverse_flow_qty': net_adverse,
        'flow_floor_qty': flow_floor,
        'price_confirmed': price_confirmed,
        'flow_corroborated': flow_corroborated,
        'confirmed_for_veto': price_confirmed and flow_corroborated,
        'confirmation_version': 'WALL_PRICE_FLOW_V2',
    }


def _log_market_event(state, kind, side, message):
    """Giữ log hữu ích mà không flood terminal khi depth biến động mạnh."""
    import time
    now = time.time()
    key = kind
    last = float(getattr(state, 'market_log_times', {}).get(key, 0.0))
    if now - last >= 5.0:
        state.market_log_times[key] = now
        logging.warning(message)

def cap_nhat_so_lenh(snapshot: dict, state):
    """
    Tính OBI và check bẫy sổ lệnh (Wall Pull / Absorption).
    Sử dụng Dynamic Threshold: Tường rút >= max(20%, 2 BTC)
    Cross-check với aggTrade event-time trong cửa sổ thích nghi 0.5–1.0s.
    """
    bids = snapshot.get('bids', [])
    asks = snapshot.get('asks', [])
    
    if not bids or not asks:
        return
        
    # Tính Tổng Vol Bids và Asks (Ép kiểu float vì API trả về string)
    bid_vol = sum(float(qty) for price, qty in bids)
    ask_vol = sum(float(qty) for price, qty in asks)
    bid_vol_top3 = sum(float(qty) for price, qty in bids[:3])
    ask_vol_top3 = sum(float(qty) for price, qty in asks[:3])
    bid_vol_top10 = sum(float(qty) for price, qty in bids[:10])
    ask_vol_top10 = sum(float(qty) for price, qty in asks[:10])
    current_mid = _book_mid(bids, asks, state)
    
    # 1. Tính OBI
    tong_vol = bid_vol + ask_vol
    previous_obi = float(getattr(state, 'obi', 0.0) or 0.0)
    if tong_vol > 0:
        obi = (bid_vol - ask_vol) / tong_vol
        state.obi = obi
    top3_total = bid_vol_top3 + ask_vol_top3
    state.obi_top3 = (
        (bid_vol_top3 - ask_vol_top3) / top3_total
        if top3_total > 0.0 else 0.0
    )
    top10_total = bid_vol_top10 + ask_vol_top10
    state.obi_top10 = (
        (bid_vol_top10 - ask_vol_top10) / top10_total
        if top10_total > 0.0 else 0.0
    )
        
    # --- WARM-UP ---
    # Bỏ qua 10s đầu tiên để lấy baseline chuẩn, tránh báo động giả khi vừa bật
    import time
    now_sec = time.time()
    state.obi_history.append((now_sec, state.obi))
    state.decision_revision = getattr(state, 'decision_revision', 0) + 1

    wall_pull = getattr(state, 'wall_pull_flag', {'active': False, 'ts': 0.0})
    if wall_pull.get('active') and now_sec - wall_pull.get('ts', 0.0) > 1.0:
        state.wall_pull_flag = {'active': False, 'side': None, 'ts': 0.0}

    absorption = getattr(state, 'absorption_event', {'active': False, 'ts': 0.0})
    if absorption.get('active') and now_sec - absorption.get('ts', 0.0) > 5.0:
        state.absorption_event = {'active': False, 'side': None, 'ts': 0.0}
        state.absorption_flag = False
    if getattr(state, 'start_time', 0) == 0:
        state.start_time = now_sec
    if now_sec - state.start_time < 10:
        # Cập nhật baseline nhưng không tính toán drop
        state.prev_bids_dict = {float(p): float(q) for p, q in bids}
        state.prev_asks_dict = {float(p): float(q) for p, q in asks}
        state.so_lenh_cvd_sell_snapshot = getattr(state, 'cvd_sell', 0.0)
        state.so_lenh_cvd_buy_snapshot = getattr(state, 'cvd_buy', 0.0)
        state.prev_so_lenh_time_s = snapshot.get('timestamp', 0)
        return
        
    # Lấy state cũ
    prev_bids_dict = getattr(state, 'prev_bids_dict', {})
    prev_asks_dict = getattr(state, 'prev_asks_dict', {})
    prev_cvd_sell_snapshot = getattr(state, 'so_lenh_cvd_sell_snapshot', 0.0)
    prev_cvd_buy_snapshot = getattr(state, 'so_lenh_cvd_buy_snapshot', 0.0)
    
    # State mới
    curr_bids_dict = {float(p): float(q) for p, q in bids}
    curr_asks_dict = {float(p): float(q) for p, q in asks}
    
    # --- CROSS-CHECK SỔ LỆNH VS aggTrade EVENT-TIME 0.5–1.0s ---
    current_time_s = snapshot.get('timestamp', 0)
    
    prev_time_s = getattr(state, 'prev_so_lenh_time_s', 0.0)
    dt_s = current_time_s - prev_time_s
    
    pending_pulls = getattr(state, 'pending_pulls', [])
    
    if prev_time_s > 0.0 and dt_s <= 0.2:
        # A. Kiểm tra Bẫy Tường Buy
        bid_drop = 0.0
        if curr_bids_dict and prev_bids_dict:
            min_curr_bid = min(curr_bids_dict.keys())
            
            for price, prev_qty in prev_bids_dict.items():
                if price >= min_curr_bid:
                    curr_qty = curr_bids_dict.get(price, 0.0)
                    if prev_qty > curr_qty:
                        bid_drop += (prev_qty - curr_qty)
                        
            if bid_drop > 0:
                threshold_bid = max(0.2 * sum(prev_bids_dict.values()), 2.0)
                if bid_drop >= threshold_bid:
                    # Ghi nhận drop nhưng CHƯA xử lý ngay, cho vào hàng chờ (pending check)
                    pending_pulls.append({
                        'side': 'buy',
                        'drop': bid_drop,
                        'cvd_start': prev_cvd_sell_snapshot,
                        'cvd_buy_start': prev_cvd_buy_snapshot,
                        'cvd_sell_start': prev_cvd_sell_snapshot,
                        'time_s': current_time_s,
                        'window_start_s': prev_time_s - TRADE_EVENT_LOOKBACK_SECONDS,
                        'obi_before': previous_obi,
                        'reference_price': current_mid,
                        'atr_at_pull': float(getattr(state, 'atr_1m', 0.0) or 0.0),
                    })

        # B. Kiểm tra Bẫy Tường Sell
        ask_drop = 0.0
        if curr_asks_dict and prev_asks_dict:
            max_curr_ask = max(curr_asks_dict.keys())
            
            for price, prev_qty in prev_asks_dict.items():
                if price <= max_curr_ask:
                    curr_qty = curr_asks_dict.get(price, 0.0)
                    if prev_qty > curr_qty:
                        ask_drop += (prev_qty - curr_qty)
                        
            if ask_drop > 0:
                threshold_ask = max(0.2 * sum(prev_asks_dict.values()), 2.0)
                if ask_drop >= threshold_ask:
                    # Ghi nhận drop nhưng CHƯA xử lý ngay, cho vào hàng chờ
                    pending_pulls.append({
                        'side': 'sell',
                        'drop': ask_drop,
                        'cvd_start': prev_cvd_buy_snapshot,
                        'cvd_buy_start': prev_cvd_buy_snapshot,
                        'cvd_sell_start': prev_cvd_sell_snapshot,
                        'time_s': current_time_s,
                        'window_start_s': prev_time_s - TRADE_EVENT_LOOKBACK_SECONDS,
                        'obi_before': previous_obi,
                        'reference_price': current_mid,
                        'atr_at_pull': float(getattr(state, 'atr_1m', 0.0) or 0.0),
                    })

    # XỬ LÝ HÀNG CHỜ: xác nhận absorption sớm từ 500ms, chờ tối đa 1s
    # trước khi kết luận wall pull. Như vậy feed aggTrade trễ không bị hụt oan.
    new_pending = []
    for pull in pending_pulls:
        age = current_time_s - pull['time_s']
        if age >= ABSORPTION_CONFIRM_MIN_SECONDS:
            buy_flow, sell_flow = _matched_flow(state, pull, current_time_s)
            if pull['side'] == 'buy':
                sell_vol_matched = sell_flow
                obi_before = float(pull.get('obi_before', 0.0))
                current_obi = float(getattr(state, 'obi', 0.0) or 0.0)
                material_book_shift = current_obi <= obi_before - 0.10 or current_obi <= -0.15
                if (
                    age >= ABSORPTION_CONFIRM_MAX_SECONDS
                    and sell_vol_matched < pull['drop'] * 0.3
                    and material_book_shift
                ):
                    state.market_event_sequence += 1
                    confirmation = _wall_confirmation(
                        state, pull, current_mid, buy_flow, sell_flow
                    )
                    state.wall_pull_flag = {
                        'active': True, 'side': 'buy', 'ts': current_time_s,
                        'event_id': f"wall:{state.market_event_sequence}:buy",
                        'classification': (
                            'CONFIRMED_TOXIC_WALL_PULL'
                            if confirmation['confirmed_for_veto']
                            else 'UNCONFIRMED_WALL_PULL'
                        ),
                        'wall_drop': pull['drop'],
                        'matched_qty': sell_vol_matched,
                        'obi_before': obi_before,
                        'obi_after': current_obi,
                        'correlation_window_ms': int(age * 1000),
                        **confirmation,
                    }
                    _log_market_event(
                        state, 'wall', 'buy',
                        f"⚠️ [WALL PULL] Buy rút {pull['drop']:.2f} BTC; "
                        f"khớp {sell_vol_matched:.2f}; OBI {obi_before:.2f}→{current_obi:.2f}",
                    )
                elif sell_vol_matched >= pull['drop'] * 0.7:
                    state.absorption_flag = True
                    state.market_event_sequence += 1
                    state.absorption_event = {
                        'active': True, 'side': 'buy', 'ts': current_time_s,
                        'event_id': f"abs:{state.market_event_sequence}:buy",
                        'reference_price': (
                            float(getattr(state, 'best_bid', 0.0))
                            + float(getattr(state, 'best_ask', 0.0))
                        ) / 2.0,
                        'atr_at_event': float(getattr(state, 'atr_1m', 0.0) or 0.0),
                        'wall_drop': pull['drop'],
                        'matched_qty': sell_vol_matched,
                        'correlation_window_ms': int(age * 1000),
                    }
                    _log_market_event(
                        state, 'absorption', 'buy',
                        f"🔥 [ABSORPTION] Buy wall {pull['drop']:.2f} BTC bị Market Sell hấp thụ.",
                    )
                elif age < ABSORPTION_CONFIRM_MAX_SECONDS:
                    new_pending.append(pull)
            else: # sell
                buy_vol_matched = buy_flow
                obi_before = float(pull.get('obi_before', 0.0))
                current_obi = float(getattr(state, 'obi', 0.0) or 0.0)
                material_book_shift = current_obi >= obi_before + 0.10 or current_obi >= 0.15
                if (
                    age >= ABSORPTION_CONFIRM_MAX_SECONDS
                    and buy_vol_matched < pull['drop'] * 0.3
                    and material_book_shift
                ):
                    state.market_event_sequence += 1
                    confirmation = _wall_confirmation(
                        state, pull, current_mid, buy_flow, sell_flow
                    )
                    state.wall_pull_flag = {
                        'active': True, 'side': 'sell', 'ts': current_time_s,
                        'event_id': f"wall:{state.market_event_sequence}:sell",
                        'classification': (
                            'CONFIRMED_TOXIC_WALL_PULL'
                            if confirmation['confirmed_for_veto']
                            else 'UNCONFIRMED_WALL_PULL'
                        ),
                        'wall_drop': pull['drop'],
                        'matched_qty': buy_vol_matched,
                        'obi_before': obi_before,
                        'obi_after': current_obi,
                        'correlation_window_ms': int(age * 1000),
                        **confirmation,
                    }
                    _log_market_event(
                        state, 'wall', 'sell',
                        f"⚠️ [WALL PULL] Sell rút {pull['drop']:.2f} BTC; "
                        f"khớp {buy_vol_matched:.2f}; OBI {obi_before:.2f}→{current_obi:.2f}",
                    )
                elif buy_vol_matched >= pull['drop'] * 0.7:
                    state.absorption_flag = True
                    state.market_event_sequence += 1
                    state.absorption_event = {
                        'active': True, 'side': 'sell', 'ts': current_time_s,
                        'event_id': f"abs:{state.market_event_sequence}:sell",
                        'reference_price': (
                            float(getattr(state, 'best_bid', 0.0))
                            + float(getattr(state, 'best_ask', 0.0))
                        ) / 2.0,
                        'atr_at_event': float(getattr(state, 'atr_1m', 0.0) or 0.0),
                        'wall_drop': pull['drop'],
                        'matched_qty': buy_vol_matched,
                        'correlation_window_ms': int(age * 1000),
                    }
                    _log_market_event(
                        state, 'absorption', 'sell',
                        f"🔥 [ABSORPTION] Sell wall {pull['drop']:.2f} BTC bị Market Buy hấp thụ.",
                    )
                elif age < ABSORPTION_CONFIRM_MAX_SECONDS:
                    new_pending.append(pull)
        else:
            new_pending.append(pull)
            
    state.pending_pulls = deque(new_pending, maxlen=20)

    # Cập nhật state cũ cho vòng lặp 100ms tiếp theo
    state.prev_bids_dict = curr_bids_dict
    state.prev_asks_dict = curr_asks_dict
    state.so_lenh_cvd_sell_snapshot = getattr(state, 'cvd_sell', 0.0)
    state.so_lenh_cvd_buy_snapshot = getattr(state, 'cvd_buy', 0.0)
    state.prev_so_lenh_time_s = current_time_s
