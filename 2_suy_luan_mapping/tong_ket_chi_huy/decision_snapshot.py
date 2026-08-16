"""Ảnh chụp nhất quán phục vụ một lần ra quyết định."""

import copy
import os
import time
from types import SimpleNamespace


FEED_MAX_AGE = {
    'thoi_gian_tick_cuoi': 3.0,
    'thoi_gian_so_lenh_cuoi': 5.0,
    'thoi_gian_dong_tien_cuoi': 5.0,
    'thoi_gian_nen_cuoi': max(
        5.0, float(os.getenv('SMC_KLINE_MAX_AGE_SECONDS', '8.0'))
    ),
    'thoi_gian_vi_mo_cuoi': 15.0,
    'execution_price_time': 3.0,
}

DECISION_FIELDS = (
    'system_ready', 'trading_enabled', 'co_lenh_mo', 'best_bid', 'best_ask',
    'current_vol_3s', 'vol_pct90', 'current_cvd_sell_3s',
    'current_cvd_buy_3s', 'cvd_buy_30m', 'cvd_sell_30m', 'p95_value',
    'fp_last_imbalance', 'sweep_m1', 'breakout_m1', 'absorption_event',
    'absorption_reaction', 'flow_divergence', 'value_area_sweep',
    'persistent_flow', 'flow_price_trap', 'zone_reaction',
    'zone_acceptance_trap',
    'wall_pull_flag', 'obi', 'obi_top3', 'obi_top10', 'obi_history',
    'continuous_m15', 'continuous_sweep_m1', 'continuous_breakout_m1',
    'continuous_footprint', 'continuous_persistent_flow',
    'continuous_zone_reaction', 'continuous_flow_divergence',
    'continuous_absorption_reaction', 'continuous_value_area_sweep',
    'continuous_evidence_revision',
    'adverse_flow_memory_by_bias',
    'open_interest', 'funding_rate', 'macro_bias',
    'positioning_cvd_divergence', 'liquidation_recovery',
    'thoi_gian_vi_mo_cuoi', 'thoi_gian_tick_cuoi',
    'thoi_gian_so_lenh_cuoi', 'thoi_gian_dong_tien_cuoi',
    'thoi_gian_nen_cuoi', 'atr_1m', 'poc', 'vah', 'val',
    'volume_profile_updated_at', 'volume_profile_coverage',
    'poc_movement_atr',
    'trend_m15', 'swing_high_m15', 'swing_low_m15', 'decision_revision',
    'structure_version', 'structure_transition', 'structure_broken_level',
    'structure_break_streak',
    'consumed_market_events',
    'execution_best_bid', 'execution_best_ask', 'execution_price_time',
    'bids_top_10', 'asks_top_10', 'balance_usdt', 'exchange_filters',
)


def _closed_extrema(rows, timeframe, wall_time, limit):
    """Compact local extrema from candles that were closed at snapshot time."""
    clean = []
    source = list(rows or ())
    for source_index, candle in enumerate(source):
        try:
            if isinstance(candle, dict):
                high = float(candle.get('h', candle.get('high', 0.0)) or 0.0)
                low = float(candle.get('l', candle.get('low', 0.0)) or 0.0)
                close_ms = float(
                    candle.get('close_time', candle.get('T', 0.0)) or 0.0
                )
                open_ms = float(
                    candle.get('open_time', candle.get('t', 0.0)) or 0.0
                )
            else:
                high, low = float(candle[2]), float(candle[3])
                open_ms = float(candle[0]) if len(candle) > 0 else 0.0
                close_ms = float(candle[6]) if len(candle) > 6 else 0.0
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if high <= 0.0 or low <= 0.0 or high < low:
            continue
        # Missing close_time is accepted only for rows before the final row;
        # explicit future/open candles are never included.
        if close_ms > 0.0 and close_ms > wall_time * 1000.0:
            continue
        if close_ms <= 0.0 and source_index == len(source) - 1:
            continue
        clean.append((high, low, open_ms, close_ms))
    extrema = []
    for index in range(1, len(clean) - 1):
        high, low, open_ms, close_ms = clean[index]
        previous, following = clean[index - 1], clean[index + 1]
        is_high = high >= previous[0] and high >= following[0]
        is_low = low <= previous[1] and low <= following[1]
        if not (is_high or is_low):
            continue
        extrema.append({
            'timeframe': timeframe,
            'high': high if is_high else 0.0,
            'low': low if is_low else 0.0,
            'open_time_ms': open_ms,
            'close_time_ms': close_ms,
        })
    return extrema[-limit:]


def capture(state, setup=None, wall_time=None, monotonic_time=None):
    """Copy toàn bộ input trước khi score để không trộn nhiều thời điểm."""
    wall_time = time.time() if wall_time is None else wall_time
    monotonic_time = time.monotonic() if monotonic_time is None else monotonic_time
    values = {
        field: copy.deepcopy(getattr(state, field, None))
        for field in DECISION_FIELDS
    }
    values['snapshot_time'] = wall_time
    values['snapshot_mono'] = monotonic_time
    history = [
        dict(item) for item in list(
            getattr(state, 'trend_price_history', ()) or ()
        )
        if isinstance(item, dict)
        and float(item.get('ts', 0.0) or 0.0) <= wall_time + 1e-6
        and float(item.get('ts', 0.0) or 0.0) >= wall_time - 190.0
    ]
    current_price = (
        (float(values.get('best_bid') or 0.0) + float(values.get('best_ask') or 0.0)) / 2.0
        if float(values.get('best_bid') or 0.0) > 0.0
        and float(values.get('best_ask') or 0.0) > 0.0
        else max(
            float(values.get('best_bid') or 0.0),
            float(values.get('best_ask') or 0.0),
        )
    )
    target = wall_time - 3.0
    start = min(history, key=lambda item: abs(float(item.get('ts', 0.0)) - target)) if history else None
    atr = float(values.get('atr_1m') or 0.0)
    values['price_progress_atr_3s'] = (
        (current_price - float(start.get('price', current_price))) / atr
        if start is not None and atr > 0.0 else 0.0
    )
    values['price_progress_coverage_3s'] = (
        max(0.0, wall_time - float(start.get('ts', wall_time)))
        if start is not None else 0.0
    )
    flow_history = [
        dict(item) for item in list(getattr(state, 'flow_1s_buffer', ()) or ())
        if isinstance(item, dict)
        and float(item.get('ts', 0.0) or 0.0) <= wall_time + 1e-6
        and float(item.get('ts', 0.0) or 0.0) >= wall_time - 190.0
    ]
    momentum_horizons = {}
    vah = float(values.get('vah') or 0.0)
    val = float(values.get('val') or 0.0)
    for horizon in (15, 60, 180):
        cutoff = wall_time - float(horizon)
        price_rows = [row for row in history if float(row.get('ts', 0.0)) >= cutoff]
        flow_rows = [row for row in flow_history if float(row.get('ts', 0.0)) >= cutoff]
        first = price_rows[0] if price_rows else None
        prices = [float(row.get('price', 0.0) or 0.0) for row in price_rows]
        prices = [value for value in prices if value > 0.0]
        progress = (
            (current_price - float(first.get('price', current_price))) / atr
            if first is not None and atr > 0.0 else 0.0
        )
        price_range = (
            (max(prices) - min(prices)) / atr
            if prices and atr > 0.0 else 0.0
        )
        buy = sum(float(row.get('buy', 0.0) or 0.0) for row in flow_rows)
        sell = sum(float(row.get('sell', 0.0) or 0.0) for row in flow_rows)
        total = buy + sell
        flow_imbalance = (buy - sell) / total if total > 0.0 else 0.0
        price_coverage = (
            max(0.0, wall_time - float(first.get('ts', wall_time)))
            if first is not None else 0.0
        )
        flow_coverage = (
            max(0.0, wall_time - float(flow_rows[0].get('ts', wall_time)))
            if flow_rows else 0.0
        )
        outside_long = (
            sum(1 for value in prices if vah > 0.0 and value >= vah) / len(prices)
            if prices else 0.0
        )
        outside_short = (
            sum(1 for value in prices if val > 0.0 and value <= val) / len(prices)
            if prices else 0.0
        )
        momentum_horizons[str(horizon)] = {
            'price_progress_atr': progress,
            'range_expansion_atr': price_range,
            'price_efficiency': (
                min(1.0, abs(progress) / max(price_range, 1e-9))
                if price_range > 0.0 else 0.0
            ),
            'price_coverage_seconds': price_coverage,
            'flow_buy': buy, 'flow_sell': sell,
            'flow_total': total, 'flow_imbalance': flow_imbalance,
            'flow_coverage_seconds': flow_coverage,
            'acceptance_long': outside_long,
            'acceptance_short': outside_short,
        }
    values['momentum_horizons'] = momentum_horizons
    values['momentum_history_seconds'] = min(
        190.0,
        max(0.0, wall_time - float(history[0].get('ts', wall_time)))
        if history else 0.0,
    )
    values['setup_id'] = setup.get('setup_id') if setup else None
    values['setup_generation'] = setup.get('generation', 0) if setup else 0
    values['setup_mode'] = setup.get('mode') if setup else None
    values['setup_bias'] = setup.get('bias') if setup else None
    values['setup_zone'] = copy.deepcopy(setup.get('zone')) if setup else None
    values['setup_zone_id'] = setup.get('zone_id') if setup else None
    values['setup_kind'] = setup.get('kind') if setup else None
    values['setup_activation_reason'] = (
        setup.get('activation_reason') if setup else None
    )
    values['setup_semantic_key'] = setup.get('semantic_key') if setup else None
    values['setup_opportunity_id'] = setup.get('opportunity_id') if setup else None
    values['setup_entry_style'] = setup.get('entry_style') if setup else None
    values['setup_breakout_target'] = setup.get('breakout_target', 0.0) if setup else 0.0
    values['setup_breakout_target2'] = setup.get('breakout_target2', 0.0) if setup else 0.0
    values['setup_breakout_target_basis'] = setup.get('breakout_target_basis') if setup else None
    values['closed_m1_extrema'] = _closed_extrema(
        getattr(state, 'klines_m1', ()), 'M1', wall_time, 24,
    )
    values['closed_m15_extrema'] = _closed_extrema(
        getattr(state, 'klines_m15', ()), 'M15', wall_time, 16,
    )
    return SimpleNamespace(**values)


def freshness(snapshot):
    now = float(snapshot.snapshot_time)
    stale = []
    for field, max_age in FEED_MAX_AGE.items():
        timestamp = float(getattr(snapshot, field, 0.0) or 0.0)
        if timestamp <= 0.0 or now - timestamp > max_age:
            stale.append(field)
    if stale:
        return False, 'Feed stale: ' + ', '.join(stale)
    if float(getattr(snapshot, 'atr_1m', 0.0) or 0.0) <= 0.0:
        return False, 'ATR chưa sẵn sàng'
    return True, 'FRESH'


def event_ids(snapshot):
    ids = []
    for field in ('fp_last_imbalance', 'sweep_m1', 'breakout_m1', 'absorption_event'):
        event = getattr(snapshot, field, {}) or {}
        event_id = event.get('event_id')
        if event_id:
            ids.append(event_id)
    return tuple(dict.fromkeys(ids))
