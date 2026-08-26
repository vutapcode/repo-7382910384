"""Adaptive shadow sizing with optional execution-filter realism and no fabricated venue specs."""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
import os

VERSION = "SHADOW_DYNAMIC_SIZING_V2_EXECUTION_REALISM"

_TARGET_QTY_BTC = float(os.getenv("SMC_FIXED_QTY_BTC", "0.001"))
_RESERVE_RATIO = 0.98
_MIN_INTERNAL_QTY_BTC = 1e-6
_FILTER_ENV = {
    "step_btc": "SMC_SHADOW_QTY_STEP_BTC",
    "min_qty_btc": "SMC_SHADOW_MIN_QTY_BTC",
    "min_notional_usdt": "SMC_SHADOW_MIN_NOTIONAL_USDT",
}


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


def _filter_config():
    values = {}
    missing = []
    invalid = []
    for key, env_name in _FILTER_ENV.items():
        raw = os.getenv(env_name)
        if raw is None or not str(raw).strip():
            missing.append(key)
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            invalid.append(key)
            continue
        if value <= 0.0:
            invalid.append(key)
            continue
        values[key] = value

    verified = not missing and not invalid
    if verified:
        mode = "VERIFIED_FILTERS"
    elif invalid:
        mode = "INVALID_FILTER_CONFIG"
    elif len(missing) == len(_FILTER_ENV):
        mode = "UNVERIFIED_FILTERS"
    else:
        mode = "PARTIAL_FILTERS"

    return {
        "mode": mode,
        "verified": verified,
        "values": values,
        "missing": tuple(missing),
        "invalid": tuple(invalid),
    }


def _quantize_down(qty, step):
    qty_d = Decimal(str(max(0.0, float(qty))))
    step_d = Decimal(str(float(step)))
    if step_d <= 0:
        return 0.0
    units = (qty_d / step_d).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * step_d)


def _apply_execution_filters(qty, price):
    cfg = _filter_config()
    meta = {
        "mode": cfg["mode"],
        "enforced": bool(cfg["verified"]),
        "missing": list(cfg["missing"]),
        "invalid": list(cfg["invalid"]),
    }
    if not cfg["verified"]:
        meta["executable"] = None
        return max(0.0, float(qty)), meta

    values = cfg["values"]
    filtered = _quantize_down(qty, values["step_btc"])
    notional = filtered * max(0.0, float(price))
    executable = (
        filtered >= values["min_qty_btc"]
        and notional >= values["min_notional_usdt"]
        and filtered > 0.0
    )
    meta.update(
        {
            "step_btc": values["step_btc"],
            "min_qty_btc": values["min_qty_btc"],
            "min_notional_usdt": values["min_notional_usdt"],
            "filtered_qty_btc": filtered,
            "notional_usdt": notional,
            "executable": executable,
        }
    )
    return filtered, meta


def _set_feasible(result, value):
    if "feasible" in result:
        result["feasible"] = bool(value)
    elif "ok" in result:
        result["ok"] = bool(value)
    else:
        result["feasible"] = bool(value)


def install(shadow):
    if getattr(shadow, "_dynamic_shadow_sizing_installed", False):
        return VERSION

    original_feasibility = shadow._entry_feasibility
    original_close = shadow._close_shadow

    def feasibility(price):
        target = max(_MIN_INTERNAL_QTY_BTC, _TARGET_QTY_BTC)
        affordable = _affordable_qty(shadow, price)
        raw_qty = min(target, affordable)
        filtered_qty, filter_meta = _apply_execution_filters(raw_qty, price)

        if filter_meta["enforced"]:
            shadow.QTY_BTC = filtered_qty
        else:
            shadow.QTY_BTC = raw_qty if raw_qty >= _MIN_INTERNAL_QTY_BTC else target

        result = original_feasibility(price)
        result["target_qty_btc"] = target
        result["raw_adaptive_qty_btc"] = float(raw_qty)
        result["adaptive_qty_btc"] = float(shadow.QTY_BTC)
        result["sizing_mode"] = "TARGET" if shadow.QTY_BTC >= target else "BALANCE_ADAPTIVE"
        result["execution_filters"] = filter_meta

        if filter_meta["enforced"] and not filter_meta["executable"]:
            _set_feasible(result, False)
            result["sizing_mode"] = "EXECUTION_FILTER_REJECT"
            result["reason"] = "SHADOW_EXECUTION_FILTER_REJECT"

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
