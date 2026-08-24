"""Verified Binance account fees plus execution-aware Tier-S cost estimates."""
import os


VERSION = "VERIFIED_COST_MODEL_V2_NO_POST_FILL_DOUBLE_COUNT"
DEFAULT_FALLBACK_FEE_BPS_PER_SIDE = 9.0
MAX_SANE_FEE_BPS_PER_SIDE = 20.0


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def refresh_account_commission(api, state, symbol="BTCUSDT",
                                     fallback_per_side=None):
    """Read account-specific Futures commission without mutating the account."""
    fallback = max(0.0, _f(
        fallback_per_side, DEFAULT_FALLBACK_FEE_BPS_PER_SIDE
    ))
    state.mainnet_maker_fee_bps = fallback
    state.mainnet_taker_fee_bps = fallback
    state.mainnet_worst_roundtrip_fee_bps = 2.0 * fallback
    state.mainnet_commission_verified = False
    state.mainnet_commission_source = "CONSERVATIVE_CONFIG_FALLBACK"

    if not bool(getattr(api, "has_private_credentials", False)):
        return {
            "verified": False,
            "source": state.mainnet_commission_source,
            "maker_fee_bps": fallback,
            "taker_fee_bps": fallback,
            "reason": "PRIVATE_CREDENTIALS_UNAVAILABLE",
        }

    try:
        payload, status = await api.get_commission_rate(symbol)
    except Exception as exc:
        return {
            "verified": False,
            "source": state.mainnet_commission_source,
            "maker_fee_bps": fallback,
            "taker_fee_bps": fallback,
            "reason": f"COMMISSION_READ_FAILED:{type(exc).__name__}",
        }

    if status != 200 or not isinstance(payload, dict):
        return {
            "verified": False,
            "source": state.mainnet_commission_source,
            "maker_fee_bps": fallback,
            "taker_fee_bps": fallback,
            "reason": "COMMISSION_RESPONSE_INVALID",
        }
    maker = _f(payload.get("makerCommissionRate"), -1.0) * 10000.0
    taker = _f(payload.get("takerCommissionRate"), -1.0) * 10000.0
    sane = (
        0.0 <= maker <= MAX_SANE_FEE_BPS_PER_SIDE
        and 0.0 < taker <= MAX_SANE_FEE_BPS_PER_SIDE
    )
    if not sane:
        return {
            "verified": False,
            "source": state.mainnet_commission_source,
            "maker_fee_bps": fallback,
            "taker_fee_bps": fallback,
            "reason": "COMMISSION_RESPONSE_INVALID",
        }

    state.mainnet_maker_fee_bps = maker
    state.mainnet_taker_fee_bps = taker
    state.mainnet_worst_roundtrip_fee_bps = 2.0 * taker
    state.mainnet_commission_verified = True
    state.mainnet_commission_source = "BINANCE_ACCOUNT_COMMISSION_RATE"
    return {
        "verified": True,
        "source": state.mainnet_commission_source,
        "maker_fee_bps": maker,
        "taker_fee_bps": taker,
        "reason": "VERIFIED",
    }


def estimate(result, state):
    """Estimate round-trip cost from intended entry style and live Futures BBO."""
    phase = str((result or {}).get("phase") or "").upper()
    execution_style = "TAKER" if phase == "RELEASE" else "MAKER"
    verified = bool(getattr(state, "mainnet_commission_verified", False))
    fallback = max(0.0, _f(
        os.getenv("SMC_SHADOW_FEE_BPS_PER_SIDE"),
        DEFAULT_FALLBACK_FEE_BPS_PER_SIDE,
    ))
    maker = _f(getattr(state, "mainnet_maker_fee_bps", fallback), fallback)
    taker = _f(getattr(state, "mainnet_taker_fee_bps", fallback), fallback)
    if not verified or maker < 0.0 or taker <= 0.0:
        maker = taker = fallback
        source = "CONSERVATIVE_CONFIG_FALLBACK"
        verified = False
    else:
        source = str(
            getattr(state, "mainnet_commission_source", "")
            or "BINANCE_ACCOUNT_COMMISSION_RATE"
        )

    bid = _f(getattr(state, "execution_best_bid", 0.0))
    ask = _f(getattr(state, "execution_best_ask", 0.0))
    mid = (bid + ask) / 2.0 if bid > 0.0 and ask > bid else 0.0
    half_spread = ((ask - bid) / mid * 5000.0) if mid > 0.0 else _f(
        os.getenv("SMC_COST_FALLBACK_HALF_SPREAD_BPS"), 1.0
    )
    market_slippage = max(0.0, _f(
        os.getenv("SMC_SHADOW_MARKET_SLIPPAGE_BPS"), 1.5
    ))
    entry_fee = taker if execution_style == "TAKER" else maker
    exit_fee = taker
    entry_slippage = half_spread + market_slippage if execution_style == "TAKER" else 0.0
    exit_slippage = half_spread + market_slippage
    total = entry_fee + exit_fee + entry_slippage + exit_slippage
    minimum_net = max(0.0, _f(os.getenv(
        "SMC_MAINNET_MARKET_MIN_NET_EDGE_BPS"
        if execution_style == "TAKER"
        else "SMC_MAINNET_MAKER_MIN_NET_EDGE_BPS"
    ), 6.0 if execution_style == "TAKER" else 2.0))
    return {
        "version": VERSION,
        "execution_style": execution_style,
        "commission_verified": verified,
        "commission_source": source,
        "entry_fee_bps": round(entry_fee, 6),
        "exit_fee_bps": round(exit_fee, 6),
        "half_spread_bps": round(max(0.0, half_spread), 6),
        "entry_slippage_bps": round(entry_slippage, 6),
        "exit_slippage_bps": round(exit_slippage, 6),
        "total_cost_bps": round(total, 6),
        "minimum_net_edge_bps": round(minimum_net, 6),
    }


def shadow_execution_plan(result, state, execution_style):
    """Return the fee/cost plan for the shadow fill that actually occurred.

    The entry council may plan a maker fill but the shadow executor can fall
    back to market after TTL.  Re-evaluate the canonical verified cost model
    from the actual fill style so journal PnL and the entry cost gate use the
    same account commission source.
    """
    style = str(execution_style or "").upper()
    is_maker = style == "MAKER_TRADE_THROUGH"
    modeled = estimate(
        dict(result or {}, phase="ACCEPTANCE" if is_maker else "RELEASE"),
        state,
    )
    # Actual fill embeds entry spread/slippage. Guardian starts from that fill,
    # so its remaining recovery floor must not charge entry impact twice.
    remaining = (
        float(modeled["entry_fee_bps"])
        + float(modeled["exit_fee_bps"])
        + float(modeled["exit_slippage_bps"])
    )
    return {
        "version": "SHADOW_VERIFIED_COST_PLAN_V1",
        "execution_style": "MAKER" if is_maker else "TAKER",
        "fill_style": style or "MARKET",
        "commission_verified": bool(modeled["commission_verified"]),
        "commission_source": modeled["commission_source"],
        "entry_fee_bps": float(modeled["entry_fee_bps"]),
        "exit_fee_bps": float(modeled["exit_fee_bps"]),
        "roundtrip_fee_bps": round(
            float(modeled["entry_fee_bps"]) + float(modeled["exit_fee_bps"]),
            6,
        ),
        "entry_slippage_bps": float(modeled["entry_slippage_bps"]),
        "exit_slippage_bps": float(modeled["exit_slippage_bps"]),
        "decision_total_cost_bps": float(modeled["total_cost_bps"]),
        "entry_execution_cost_embedded_in_fill": True,
        "total_cost_bps": round(remaining, 6),
        "minimum_net_edge_bps": float(modeled["minimum_net_edge_bps"]),
    }


def position_total_cost_bps(position, fallback_bps=18.0):
    """Cost floor used by Risk for an existing position, including slippage."""
    plan = getattr(position, "shadow_cost_plan", None) or {}
    try:
        value = float(plan.get("total_cost_bps", 0.0) or 0.0)
    except (AttributeError, TypeError, ValueError):
        value = 0.0
    return value if value > 0.0 else max(0.0, float(fallback_bps or 0.0))
