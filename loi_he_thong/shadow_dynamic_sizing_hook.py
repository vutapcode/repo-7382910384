"""Adaptive shadow sizing that preserves strategy decisions while avoiding bankroll-only skips."""
from __future__ import annotations

import os

VERSION = "SHADOW_DYNAMIC_SIZING_V1"

_TARGET_QTY_BTC = float(os.getenv("SMC_FIXED_QTY_BTC", "0.001"))
_RESERVE_RATIO = 0.98
_MIN_QTY_BTC = 1e-6


def _affordable_qty(shadow, price: float) -> float:
    price = max(0.0, float(price))
    if price <= 0.0:
        return 0.0
    balance = float(
        getattr(
            shadow.app.state,
            "mainnet_shadow_balance_usdt",
            getattr(shadow, "START_BALANCE_USDT", 0.0),
         )
        or 0.0
    )
    leverage = max(1.0, float(getattr(shadow, "LEVERAGE", 1.0) or 1.0))
    fee_bps = max(0.0, float(getattr(shadow, "FEE_BPS_PER_SIDE", 0.0) or 0.0))
    required_per_btc = price * ((1.0 / leverage) + (2.0 * fee_bps / 10000.0))
    if required_per_btc <= 0.0 or balance <= 0.0:
        return 0.0
    return max(0.0, balance * _RESERVE_RATIO / required_per_btc)


def install(shadow):
    if getattr(shadow, "_dynamic_shadow_sizing_installed", False):
        return VERSION

    original_feasibility = shadow._entry_feasibility
    original_close = shadow._close_shadow

    def feasibility(price):
        target = max(_MIN_QTY_BTC, _TARGET_QTY_BTC)
        affordable = _affordable_qty(shadow, price)
        qty = min(target, affordable)
        shadow.QTY_BTC = qty if qty >= _MIN_QTY_BTC else target
        result = original_feasibility(price)
        result["target_qty_btc"] = target
        result["adaptive_qty_btc"] = float(shadow.QTY_BTC)
        result["sizing_mode"] = "TARGET" if shadow.QTY_BTC >= target else "BALANCE_ADAPTIVE"
        return result

    def close_shadow(pos, guardian_result, now):
        previous = shadow.QTY_BTC
        try:
            pos_qty = float(getattr(pos, "qty", 0.0) or 0.0)
            if pos_qty > 0.0:
                shadow.QTY_BTC = pos_qty
            return original_close(pos, guardian_result, now)
        finally:
            shadow.QTY_BTC = previous

    shadow._entry_feasibility = feasibility
    shadow._close_shadow = close_shadow
    shadow._dynamic_shadow_sizing_installed = True
    return VERSION
