"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / map_dong_tien
- ROLE: Tính toán Delta (Buy Vol - Sell Vol) và CVD tích lũy trong ngày.
- I/O: IN: RAM (Lệnh khớp) | OUT: RAM (CVD State)
- RULE: Đây là owner duy nhất của cumulative Binance Spot executed-flow counters.
"""

from datetime import datetime, timezone
import logging
import time


def _prune_cvd_30m(state, now_sec: int):
    """Loại dữ liệu quá 30 phút và trừ đúng khỏi rolling totals."""
    cutoff = now_sec - 1800
    while state.cvd_30m_buffer and state.cvd_30m_buffer[0]['ts'] < cutoff:
        old_item = state.cvd_30m_buffer.popleft()
        state.cvd_buy_30m -= old_item['buy']
        state.cvd_sell_30m -= old_item['sell']
    state.cvd_buy_30m = max(0.0, state.cvd_buy_30m)
    state.cvd_sell_30m = max(0.0, state.cvd_sell_30m)


def _update_flow_1s(state, ts_sec, buy_add, sell_add):
    """Nén trade theo event-second; bounded input cho flow ngắn hạn."""
    buffer = state.flow_1s_buffer
    if buffer and buffer[-1]['ts'] == ts_sec:
        buffer[-1]['buy'] += buy_add
        buffer[-1]['sell'] += sell_add
    elif not buffer or ts_sec > buffer[-1]['ts']:
        buffer.append({'ts': ts_sec, 'buy': buy_add, 'sell': sell_add})
    # Continuous short-horizon consumers need at most ~180s. Bias V11 gets
    # medium/long flow by differencing cumulative counters, not by retaining
    # raw trades for an arbitrary forecast horizon.
    cutoff = ts_sec - 190
    while buffer and buffer[0]['ts'] < cutoff:
        buffer.popleft()


def cap_nhat_cvd(lenh_khop: dict, state):
    """Apply one Binance Spot executed trade exactly once to canonical flow state."""
    ts_ms = lenh_khop.get('thoi_gian_ms', 0)
    if ts_ms == 0:
        ts_ms = lenh_khop.get('thoi_gian', 0)
    if ts_ms == 0:
        return

    day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
    if state.cvd_day is None:
        state.cvd_day = day
    elif day != state.cvd_day:
        state.cvd_buy = 0.0
        state.cvd_sell = 0.0
        state.cvd_day = day
        logging.info(f"🔄 [CVD] Sang ngày UTC mới ({day}) — Đã reset CVD về 0.")

    qty = float(lenh_khop['khoi_luong'])
    is_sell = bool(lenh_khop['ban_chu_dong'])
    event_ts_s = ts_ms / 1000.0
    state.last_trade_event_time_s = max(
        float(getattr(state, 'last_trade_event_time_s', 0.0) or 0.0),
        event_ts_s,
    )
    state.trade_flow_timeline.append({
        'ts': event_ts_s,
        'buy': 0.0 if is_sell else qty,
        'sell': qty if is_sell else 0.0,
    })

    # Canonical cumulative executed-cash counters. They intentionally do not
    # reset at UTC day; reconnect/sequence safety is carried by spot_flow_epoch.
    if is_sell:
        state.spot_cvd_sell_total = float(
            getattr(state, 'spot_cvd_sell_total', 0.0) or 0.0
        ) + qty
    else:
        state.spot_cvd_buy_total = float(
            getattr(state, 'spot_cvd_buy_total', 0.0) or 0.0
        ) + qty

    if is_sell:
        state.cvd_sell += qty
    else:
        state.cvd_buy += qty
    state.decision_revision = getattr(state, 'decision_revision', 0) + 1

    ts_sec = int(ts_ms / 1000)
    if is_sell:
        state.cvd_sell_30m += qty
        buy_add, sell_add = 0.0, qty
    else:
        state.cvd_buy_30m += qty
        buy_add, sell_add = qty, 0.0

    if not state.cvd_30m_buffer:
        state.cvd_30m_buffer.append({'ts': ts_sec, 'buy': buy_add, 'sell': sell_add})
    else:
        last_item = state.cvd_30m_buffer[-1]
        if last_item['ts'] == ts_sec:
            last_item['buy'] += buy_add
            last_item['sell'] += sell_add
        else:
            state.cvd_30m_buffer.append({'ts': ts_sec, 'buy': buy_add, 'sell': sell_add})

    _prune_cvd_30m(state, ts_sec)
    _update_flow_1s(state, ts_sec, buy_add, sell_add)

    if state.last_3s_window_ts == 0.0:
        state.last_3s_window_ts = ts_sec

    if ts_sec - state.last_3s_window_ts < 3:
        state.current_vol_3s += qty
        if is_sell:
            state.current_cvd_sell_3s = getattr(state, 'current_cvd_sell_3s', 0.0) + qty
        else:
            state.current_cvd_buy_3s = getattr(state, 'current_cvd_buy_3s', 0.0) + qty
    else:
        state.vol_3s_history.append(state.current_vol_3s)
        history_list = list(state.vol_3s_history)
        if len(history_list) > 10:
            sorted_vols = sorted(history_list)
            idx_90 = int(len(sorted_vols) * 0.9)
            state.vol_pct90 = sorted_vols[idx_90]

        state.last_3s_window_ts = ts_sec
        state.current_vol_3s = qty
        state.current_cvd_sell_3s = qty if is_sell else 0.0
        state.current_cvd_buy_3s = 0.0 if is_sell else qty


def kiem_tra_idle(state):
    """Reset current 3s window after idle without touching cumulative flow."""
    ts_now = int(time.time())
    _prune_cvd_30m(state, ts_now)
    last_ts = getattr(state, 'last_3s_window_ts', 0)
    if last_ts > 0 and (ts_now - last_ts) >= 3:
        if getattr(state, 'current_vol_3s', 0.0) > 0:
            state.vol_3s_history.append(state.current_vol_3s)
            history_list = list(state.vol_3s_history)
            if len(history_list) > 10:
                sorted_vols = sorted(history_list)
                idx_90 = int(len(sorted_vols) * 0.9)
                state.vol_pct90 = sorted_vols[idx_90]

        state.last_3s_window_ts = ts_now
        state.current_vol_3s = 0.0
        state.current_cvd_sell_3s = 0.0
        state.current_cvd_buy_3s = 0.0
