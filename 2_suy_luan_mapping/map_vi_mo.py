"""OI/Funding + positioning advisory; không trực tiếp mở hay đóng lệnh."""


POSITIONING_LOOKBACK_SECONDS = 300.0
POSITIONING_MIN_SPAN_SECONDS = 240.0
OI_LIQUIDATION_DROP_PCT = -0.50
OI_RECOVERY_PCT = 0.05
PRICE_EXCURSION_ATR = 1.0
PRICE_RECLAIM_ATR = 0.30
CVD_DIVERGENCE_IMBALANCE = 0.10


def _mid_price(state):
    bid = float(getattr(state, 'best_bid', 0.0) or 0.0)
    ask = float(getattr(state, 'best_ask', 0.0) or 0.0)
    return (bid + ask) / 2.0 if bid > 0.0 and ask > 0.0 else max(bid, ask)


def _event(active=False, direction=None, ts=0.0, event_id=None, **extra):
    return {
        'active': active,
        'direction': direction,
        'ts': ts,
        'event_id': event_id,
        **extra,
    }


def _positioning_window(state, now):
    history = state.macro_history
    cutoff = now - 900.0
    while history and history[0]['ts'] < cutoff:
        history.popleft()
    candidates = [item for item in history if item['ts'] >= now - POSITIONING_LOOKBACK_SECONDS]
    if len(candidates) < 2:
        return None
    if candidates[-1]['ts'] - candidates[0]['ts'] < POSITIONING_MIN_SPAN_SECONDS:
        return None
    return candidates


def _update_cvd_divergence(state, window, now, atr):
    if not window:
        state.positioning_cvd_divergence = _event(ts=now)
        return
    start, end = window[0], window[-1]
    buy_delta = max(0.0, end['cvd_buy'] - start['cvd_buy'])
    sell_delta = max(0.0, end['cvd_sell'] - start['cvd_sell'])
    total = buy_delta + sell_delta
    if total <= 0.0:
        state.positioning_cvd_divergence = _event(ts=now)
        return
    imbalance = (buy_delta - sell_delta) / total
    price_change_atr = (end['price'] - start['price']) / atr
    direction = None
    if price_change_atr <= -PRICE_EXCURSION_ATR and imbalance >= CVD_DIVERGENCE_IMBALANCE:
        direction = 'LONG'
    elif price_change_atr >= PRICE_EXCURSION_ATR and imbalance <= -CVD_DIVERGENCE_IMBALANCE:
        direction = 'SHORT'
    state.positioning_cvd_divergence = _event(
        active=direction is not None,
        direction=direction,
        ts=now,
        event_id=(f"cvddiv5m:{int(start['ts'])}:{direction}" if direction else None),
        lookback_seconds=end['ts'] - start['ts'],
        price_change_atr=price_change_atr,
        flow_imbalance=imbalance,
    )


def _update_liquidation_recovery(state, window, now, atr):
    if not window:
        state.liquidation_recovery = _event(ts=now)
        return
    min_index = min(range(len(window)), key=lambda index: window[index]['oi'])
    if min_index <= 0:
        state.liquidation_recovery = _event(ts=now)
        return
    oi_trough = window[min_index]
    oi_peak = max(window[:min_index + 1], key=lambda item: item['oi'])
    oi_drop_pct = (oi_trough['oi'] - oi_peak['oi']) / oi_peak['oi'] * 100.0
    current = window[-1]
    oi_recovery_pct = (
        (current['oi'] - oi_trough['oi']) / oi_trough['oi'] * 100.0
        if oi_trough['oi'] > 0.0 else 0.0
    )
    direction = None
    extreme = None
    if oi_drop_pct <= OI_LIQUIDATION_DROP_PCT and oi_recovery_pct >= OI_RECOVERY_PCT:
        price_start = window[0]['price']
        low_item = min(window, key=lambda item: item['price'])
        high_item = max(window, key=lambda item: item['price'])
        long_excursion = (low_item['price'] - price_start) / atr
        long_reclaim = (current['price'] - low_item['price']) / atr
        short_excursion = (high_item['price'] - price_start) / atr
        short_reclaim = (high_item['price'] - current['price']) / atr
        candidates = []
        if long_excursion <= -PRICE_EXCURSION_ATR and long_reclaim >= PRICE_RECLAIM_ATR:
            candidates.append((low_item['ts'], 'LONG', low_item))
        if short_excursion >= PRICE_EXCURSION_ATR and short_reclaim >= PRICE_RECLAIM_ATR:
            candidates.append((high_item['ts'], 'SHORT', high_item))
        if candidates:
            _, direction, extreme = max(candidates, key=lambda item: item[0])
    state.liquidation_recovery = _event(
        active=direction is not None,
        direction=direction,
        ts=now,
        event_id=(f"liqrec5m:{int(extreme['ts'])}:{direction}" if direction else None),
        lookback_seconds=window[-1]['ts'] - window[0]['ts'],
        oi_drop_pct=oi_drop_pct,
        oi_recovery_pct=oi_recovery_pct,
        extreme_price=(extreme['price'] if extreme else None),
    )


def cap_nhat_vi_mo(state):
    """Chỉ xử lý một lần cho mỗi mẫu REST mới; mọi output đều advisory/size-only."""
    oi = float(getattr(state, 'open_interest', 0.0) or 0.0)
    funding = float(getattr(state, 'funding_rate', 0.0) or 0.0)
    input_ts = float(getattr(state, 'thoi_gian_vi_mo_cuoi', 0.0) or 0.0)
    if oi <= 0.0 or input_ts <= 0.0:
        state.macro_bias = 'NEUTRAL'
        return
    if input_ts <= float(getattr(state, 'last_mapped_macro_ts', 0.0) or 0.0):
        return
    state.last_mapped_macro_ts = input_ts

    previous = float(getattr(state, 'prev_open_interest', 0.0) or 0.0)
    if previous > 0.0:
        change_pct = (oi - previous) / previous * 100.0
        state.open_interest_change_pct = change_pct
        if change_pct >= 0.01 and funding < 0:
            state.macro_bias = 'LONG'
        elif change_pct >= 0.01 and funding > 0:
            state.macro_bias = 'SHORT'
        else:
            state.macro_bias = 'NEUTRAL'
    state.prev_open_interest = oi

    price = _mid_price(state)
    atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
    if price > 0.0 and atr > 0.0:
        state.macro_history.append({
            'ts': input_ts,
            'oi': oi,
            'funding': funding,
            'price': price,
            'cvd_buy': float(getattr(state, 'cvd_buy', 0.0) or 0.0),
            'cvd_sell': float(getattr(state, 'cvd_sell', 0.0) or 0.0),
        })
        window = _positioning_window(state, input_ts)
        _update_cvd_divergence(state, window, input_ts, atr)
        _update_liquidation_recovery(state, window, input_ts, atr)
    state.decision_revision = getattr(state, 'decision_revision', 0) + 1
