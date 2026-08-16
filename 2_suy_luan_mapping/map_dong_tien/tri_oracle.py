"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / map_dong_tien / tri_oracle
- ROLE: So sanh 3 nguon CVD de phat hien thao tung (Tri-Oracle Divergence).
- I/O: IN: state.coinbase_cvd_1m, state.cvd_buy/sell, state.futures_cvd_1m
        OUT: state.tri_oracle_signal
- TIER: S — Day la bo loc cao nhat trong he thong.
- RULE: Fail-open. Neu thieu data Coinbase, ket qua la NEUTRAL (khong choc vet lenh).
"""

import time
import logging


def _tinh_futures_cvd_1m(state) -> float:
    """
    Tinh CVD Futures 1 phut tu deque danh_sach_khop_lenh_futures.
    Tra ve float. Fallback 0.0 neu chua co data.
    """
    buf = getattr(state, 'danh_sach_khop_lenh_futures', None)
    if not buf:
        return 0.0
    now_ms = time.time() * 1000
    cutoff = now_ms - 60_000
    cvd = 0.0
    for item in buf:
        if item.get('thoi_gian_ms', 0) < cutoff:
            continue
        qty = float(item.get('khoi_luong', 0.0))
        # ban_chu_dong = True => nguoi ban la maker => nguoi mua la taker => delta duong
        # Binance aggTrade: m=True => buyer is maker => seller is taker => delta am
        is_buyer_maker = bool(item.get('ban_chu_dong', False))
        cvd += -qty if is_buyer_maker else qty
    return cvd


def cap_nhat_tri_oracle(state) -> str:
    """
    Core logic Tri-Oracle Divergence.
    So sanh CVD cua 3 nguon:
      - Coinbase Spot (Chan ly to chuc — TIER S)
      - Binance Spot  (Dong tien mat that — TIER A)
      - Binance Futures (Moi nhu dam dong — TIER C)

    Tra ve signal va ghi vao state.tri_oracle_signal:
      'LONG_CONFIRMED'  — Ca 3 dong thuan Mua
      'SHORT_CONFIRMED' — Ca 3 dong thuan Ban
      'DIVERGENCE'      — Futures ngoc chieu voi Coinbase (bep trung thuc tung)
      'NEUTRAL'         — Khong du data hoac tin hieu khong ro rang
    """
    cb_cvd  = float(getattr(state, 'coinbase_cvd_1m', 0.0) or 0.0)
    sp_buy  = float(getattr(state, 'cvd_buy', 0.0) or 0.0)
    sp_sell = float(getattr(state, 'cvd_sell', 0.0) or 0.0)
    sp_cvd  = sp_buy - sp_sell  # Binance Spot CVD
    fut_cvd = _tinh_futures_cvd_1m(state)

    # Kiem tra co du data Coinbase khong
    thoi_gian_coinbase = float(getattr(state, 'thoi_gian_coinbase_cuoi', 0.0) or 0.0)
    coinbase_fresh = (time.time() - thoi_gian_coinbase) < 30.0  # Du lieu phai moi hon 30s

    # Neu Coinbase chua co data -> Fail-open (NEUTRAL, khong choc vet)
    if not coinbase_fresh or (cb_cvd == 0.0 and sp_cvd == 0.0):
        signal = 'NEUTRAL'
        state.tri_oracle_signal = signal
        return signal

    # --- PHAN KY NGUY HIEM: Futures ngoc Coinbase ---
    # Fakeout Long: Futures dang xanh (dam dong mua duoi day), nhung Coinbase dang xap (to chuc xa)
    if fut_cvd > 0 and cb_cvd < -0.5 and sp_cvd <= 0:
        signal = 'DIVERGENCE'
        logging.warning(
            f"[TRI-ORACLE] DIVERGENCE! Futures={fut_cvd:.2f} XANH nhung "
            f"Coinbase={cb_cvd:.2f} DO. Kha nang Fakeout Long cao!"
        )

    # Fakeout Short: Futures dang do (dam dong ban), nhung Coinbase dang xanh (to chuc gom)
    elif fut_cvd < 0 and cb_cvd > 0.5 and sp_cvd >= 0:
        signal = 'DIVERGENCE'
        logging.warning(
            f"[TRI-ORACLE] DIVERGENCE! Futures={fut_cvd:.2f} DO nhung "
            f"Coinbase={cb_cvd:.2f} XANH. Kha nang Fakeout Short cao!"
        )

    # XAC NHAN LONG: Coinbase + Spot cung mua
    elif cb_cvd > 0 and sp_cvd > 0:
        signal = 'LONG_CONFIRMED'

    # XAC NHAN SHORT: Coinbase + Spot cung ban
    elif cb_cvd < 0 and sp_cvd < 0:
        signal = 'SHORT_CONFIRMED'

    else:
        signal = 'NEUTRAL'

    state.tri_oracle_signal = signal
    logging.debug(
        f"[TRI-ORACLE] Signal={signal} | CB={cb_cvd:.2f} | SP={sp_cvd:.2f} | FUT={fut_cvd:.2f}"
    )
    return signal
