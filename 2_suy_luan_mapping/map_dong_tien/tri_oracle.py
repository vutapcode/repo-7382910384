"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / map_dong_tien / tri_oracle
- ROLE: So sanh 3 nguon CVD de phat hien thao tung (Tri-Oracle Divergence).
- I/O: IN: state.coinbase_cvd_1m, state.cvd_buy/sell, state.danh_sach_khop_lenh_futures
       OUT: state.tri_oracle_signal
- TIER: S — bo loc cao nhat trong he thong.
- RULE: Fail-open khi thieu Coinbase; confirmation bat buoc ca 3 nguon cung dau.
"""

import logging
import time


def _tinh_futures_cvd_1m(state) -> float:
    """Tinh CVD Futures 1 phut tu deque aggTrade; 0.0 neu chua co du lieu."""
    buf = getattr(state, 'danh_sach_khop_lenh_futures', None)
    if not buf:
        return 0.0

    cutoff = time.time() * 1000.0 - 60_000.0
    cvd = 0.0
    for item in buf:
        try:
            if float(item.get('thoi_gian_ms', 0.0) or 0.0) < cutoff:
                continue
            qty = float(item.get('khoi_luong', 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue

        # Binance aggTrade `m=True`: buyer is maker => seller is taker => negative delta.
        buyer_is_maker = bool(item.get('ban_chu_dong', False))
        cvd += -qty if buyer_is_maker else qty
    return cvd


def cap_nhat_tri_oracle(state) -> str:
    """
    So sanh Coinbase Spot, Binance Spot va Binance Futures CVD.

    LONG_CONFIRMED / SHORT_CONFIRMED chi duoc phat khi ca 3 nguon cung dau.
    DIVERGENCE duoc uu tien khi Futures nguoc chieu Coinbase va Spot khong
    phan bac dong tien Coinbase. Neu Coinbase stale/thieu du lieu thi NEUTRAL.
    """
    cb_cvd = float(getattr(state, 'coinbase_cvd_1m', 0.0) or 0.0)
    sp_buy = float(getattr(state, 'cvd_buy', 0.0) or 0.0)
    sp_sell = float(getattr(state, 'cvd_sell', 0.0) or 0.0)
    sp_cvd = sp_buy - sp_sell
    fut_cvd = _tinh_futures_cvd_1m(state)

    coinbase_ts = float(getattr(state, 'thoi_gian_coinbase_cuoi', 0.0) or 0.0)
    coinbase_fresh = coinbase_ts > 0.0 and time.time() - coinbase_ts < 30.0

    # Fail-open: khong dung Tier-S de veto/confirm khi Coinbase chua san sang.
    if not coinbase_fresh or (cb_cvd == 0.0 and sp_cvd == 0.0):
        signal = 'NEUTRAL'
        state.tri_oracle_signal = signal
        return signal

    # Futures nguoc Coinbase; Spot khong phan bac Coinbase.
    if fut_cvd > 0.0 and cb_cvd < -0.5 and sp_cvd <= 0.0:
        signal = 'DIVERGENCE'
        logging.warning(
            "[TRI-ORACLE] DIVERGENCE: FUT=%.2f > 0, CB=%.2f < 0, SP=%.2f",
            fut_cvd, cb_cvd, sp_cvd,
        )
    elif fut_cvd < 0.0 and cb_cvd > 0.5 and sp_cvd >= 0.0:
        signal = 'DIVERGENCE'
        logging.warning(
            "[TRI-ORACLE] DIVERGENCE: FUT=%.2f < 0, CB=%.2f > 0, SP=%.2f",
            fut_cvd, cb_cvd, sp_cvd,
        )
    elif cb_cvd > 0.0 and sp_cvd > 0.0 and fut_cvd > 0.0:
        signal = 'LONG_CONFIRMED'
    elif cb_cvd < 0.0 and sp_cvd < 0.0 and fut_cvd < 0.0:
        signal = 'SHORT_CONFIRMED'
    else:
        signal = 'NEUTRAL'

    state.tri_oracle_signal = signal

    # Mirror Tier-S result into a field already copied by the immutable decision
    # snapshot.  kiem_duyet_veto reads this as a backward-compatible fallback
    # until every caller is on the explicit tri_oracle_signal snapshot contract.
    flow_divergence = getattr(state, 'flow_divergence', None)
    if isinstance(flow_divergence, dict):
        mirrored = dict(flow_divergence)
    else:
        mirrored = {}
    mirrored['tri_oracle_signal'] = signal
    mirrored['tri_oracle_ts'] = time.time()
    state.flow_divergence = mirrored

    logging.debug(
        "[TRI-ORACLE] Signal=%s | CB=%.2f | SP=%.2f | FUT=%.2f",
        signal, cb_cvd, sp_cvd, fut_cvd,
    )
    return signal
