"""Verified Binance account fees plus execution-aware Tier-S cost estimates."""
import os


VERSION = "VERIFIED_COST_MODEL_V3_SHADOW_ASSUMED_PROFILE"
DEFAULT_FALLBACK_FEE_BPS_PER_SIDE = 9.0
MAX_SANE_FEE_BPS_PER_SIDE = 20.0
FROZEN_COST_PLAN_VERSION = "FROZEN_COST_PLAN_V2_SHADOW_PROFILE"


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fallback_fee_bps_per_side():
    """Return the one canonical fallback used when account fees are unknown."""
    return max(0.0, _f(
        os.getenv("SMC_SHADOW_FEE_BPS_PER_SIDE"),
        DEFAULT_FALLBACK_FEE_BPS_PER_SIDE,
    ))


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def shadow_commission_profile(state=None):
    """Return an explicit simulation-only fee profile in pure SHADOW mode.

    This is not account verification.  The profile exists so a deliberately
    locked private API does not force maker and taker into the same arbitrary
    fee or disable realistic shadow fallback execution.  Any live authority
    request disables it immediately.
    """
    profile = str(
        os.getenv("SMC_SHADOW_COMMISSION_PROFILE", "") or ""
    ).strip().upper()
    mode = str(os.getenv("WSTRADE_MODE", "") or "").strip().upper()
    live_requested = bool(
        _truthy(os.getenv("SMC_ENABLE_TRADING"))
        or _truthy(os.getenv("SMC_MAINNET_ARMED"))
        or _truthy(os.getenv("SMC_MAINNET_EXCLUSIVE_ACCOUNT"))
        or bool(getattr(state, "wstrade_live_armed", False))
    )
    if not profile or mode != "SHADOW" or live_requested:
        return None
    maker = _f(os.getenv("SMC_SHADOW_MAKER_FEE_BPS"), 2.0)
    taker = _f(os.getenv("SMC_SHADOW_TAKER_FEE_BPS"), 5.0)
    if not (
        0.0 <= maker <= MAX_SANE_FEE_BPS_PER_SIDE
        and 0.0 < taker <= MAX_SANE_FEE_BPS_PER_SIDE
        and maker <= taker
    ):
        return None
    return {
        "profile": profile,
        "maker_fee_bps": maker,
        "taker_fee_bps": taker,
        "source": "SHADOW_ASSUMED_COMMISSION_PROFILE",
        "simulation_cost_usable": True,
        "verified": False,
    }


def shadow_risk_fee_bps_per_side(state=None):
    profile = shadow_commission_profile(state)
    return (
        float(profile["taker_fee_bps"])
        if profile else fallback_fee_bps_per_side()
    )


def _set_verification_reason(state, reason):
    reason = str(reason or "COMMISSION_UNVERIFIED")
    state.mainnet_commission_verification_reason = reason
    return reason


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
    state.mainnet_commission_simulation_usable = False
    state.mainnet_commission_profile = None
    _set_verification_reason(state, "COMMISSION_CHECK_NOT_COMPLETED")

    assumed = shadow_commission_profile(state)
    if assumed:
        state.mainnet_maker_fee_bps = assumed["maker_fee_bps"]
        state.mainnet_taker_fee_bps = assumed["taker_fee_bps"]
        state.mainnet_worst_roundtrip_fee_bps = 2.0 * assumed[
            "taker_fee_bps"
        ]
        state.mainnet_commission_source = assumed["source"]
        state.mainnet_commission_simulation_usable = True
        state.mainnet_commission_profile = assumed["profile"]
        reason = _set_verification_reason(
            state, "SHADOW_PROFILE_ACTIVE_ACCOUNT_QUERY_SKIPPED"
        )
        return {
            "verified": False,
            "simulation_cost_usable": True,
            "profile": assumed["profile"],
            "source": assumed["source"],
            "maker_fee_bps": assumed["maker_fee_bps"],
            "taker_fee_bps": assumed["taker_fee_bps"],
            "reason": reason,
        }

    if not bool(getattr(api, "has_private_credentials", False)):
        reason = _set_verification_reason(
            state, "PRIVATE_CREDENTIALS_UNAVAILABLE"
        )
        return {
            "verified": False,
            "source": state.mainnet_commission_source,
            "maker_fee_bps": fallback,
            "taker_fee_bps": fallback,
            "reason": reason,
        }

    try:
        payload, status = await api.get_commission_rate(symbol)
    except Exception as exc:
        reason = _set_verification_reason(
            state, f"COMMISSION_READ_FAILED:{type(exc).__name__}"
        )
        return {
            "verified": False,
            "source": state.mainnet_commission_source,
            "maker_fee_bps": fallback,
            "taker_fee_bps": fallback,
            "reason": reason,
        }

    if status != 200 or not isinstance(payload, dict):
        exchange_code = (
            payload.get("code") if isinstance(payload, dict) else None
        )
        suffix = f"_BINANCE_{exchange_code}" if exchange_code is not None else ""
        reason = _set_verification_reason(
            state, f"COMMISSION_HTTP_{status}{suffix}"
        )
        return {
            "verified": False,
            "source": state.mainnet_commission_source,
            "maker_fee_bps": fallback,
            "taker_fee_bps": fallback,
            "reason": reason,
        }
    maker = _f(payload.get("makerCommissionRate"), -1.0) * 10000.0
    taker = _f(payload.get("takerCommissionRate"), -1.0) * 10000.0
    sane = (
        0.0 <= maker <= MAX_SANE_FEE_BPS_PER_SIDE
        and 0.0 < taker <= MAX_SANE_FEE_BPS_PER_SIDE
    )
    if not sane:
        reason = _set_verification_reason(
            state, "COMMISSION_RESPONSE_INVALID"
        )
        return {
            "verified": False,
            "source": state.mainnet_commission_source,
            "maker_fee_bps": fallback,
            "taker_fee_bps": fallback,
            "reason": reason,
        }

    state.mainnet_maker_fee_bps = maker
    state.mainnet_taker_fee_bps = taker
    state.mainnet_worst_roundtrip_fee_bps = 2.0 * taker
    state.mainnet_commission_verified = True
    state.mainnet_commission_source = "BINANCE_ACCOUNT_COMMISSION_RATE"
    state.mainnet_commission_simulation_usable = False
    state.mainnet_commission_profile = None
    reason = _set_verification_reason(state, "VERIFIED")
    return {
        "verified": True,
        "source": state.mainnet_commission_source,
        "maker_fee_bps": maker,
        "taker_fee_bps": taker,
        "reason": reason,
    }


def estimate(result, state):
    """Estimate round-trip cost from intended entry style and live Futures BBO."""
    phase = str((result or {}).get("phase") or "").upper()
    execution_style = "TAKER" if phase == "RELEASE" else "MAKER"
    verified = bool(getattr(state, "mainnet_commission_verified", False))
    fallback = fallback_fee_bps_per_side()
    maker = _f(getattr(state, "mainnet_maker_fee_bps", fallback), fallback)
    taker = _f(getattr(state, "mainnet_taker_fee_bps", fallback), fallback)
    assumed = bool(
        not verified
        and getattr(state, "mainnet_commission_simulation_usable", False)
        and shadow_commission_profile(state)
    )
    if assumed and maker >= 0.0 and taker > 0.0:
        source = str(
            getattr(state, "mainnet_commission_source", "")
            or "SHADOW_ASSUMED_COMMISSION_PROFILE"
        )
    elif not verified or maker < 0.0 or taker <= 0.0:
        maker = taker = fallback
        source = "CONSERVATIVE_CONFIG_FALLBACK"
        verified = False
    else:
        source = str(
            getattr(state, "mainnet_commission_source", "")
            or "BINANCE_ACCOUNT_COMMISSION_RATE"
        )
    verification_reason = str(
        getattr(state, "mainnet_commission_verification_reason", "")
        or ("VERIFIED" if verified else "COMMISSION_UNVERIFIED")
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
        "simulation_cost_usable": assumed,
        "commission_profile": getattr(
            state, "mainnet_commission_profile", None
        ) if assumed else None,
        "commission_source": source,
        "commission_verification_reason": verification_reason,
        "entry_fee_bps": round(entry_fee, 6),
        "exit_fee_bps": round(exit_fee, 6),
        "half_spread_bps": round(max(0.0, half_spread), 6),
        "entry_slippage_bps": round(entry_slippage, 6),
        "exit_slippage_bps": round(exit_slippage, 6),
        "total_cost_bps": round(total, 6),
        "minimum_net_edge_bps": round(minimum_net, 6),
    }


def freeze_execution_cost_contract(result, state):
    """Freeze style-specific executable costs from one decision-time BBO.

    Maker and taker are deliberately separate. Comparing a market fallback to
    a maker budget would systematically cancel valid fallbacks and manufacture
    misses rather than detect spread deterioration.
    """
    maker = estimate(dict(result or {}, phase="ACCEPTANCE"), state)
    taker = estimate(dict(result or {}, phase="RELEASE"), state)
    return {
        "version": "EXECUTION_COST_CONTRACT_V1_STYLE_SPECIFIC",
        "commission_verified": bool(
            maker.get("commission_verified") and taker.get("commission_verified")
        ),
        "simulation_cost_usable": bool(
            maker.get("simulation_cost_usable")
            and taker.get("simulation_cost_usable")
        ),
        "commission_profile": maker.get("commission_profile"),
        "commission_source": maker.get("commission_source"),
        "commission_verification_reason": maker.get(
            "commission_verification_reason"
        ),
        "budgets_bps": {
            "MAKER": float(maker.get("total_cost_bps", 0.0) or 0.0),
            "TAKER": float(taker.get("total_cost_bps", 0.0) or 0.0),
        },
        "decision_components": {"MAKER": maker, "TAKER": taker},
    }


def validate_execution_cost_contract(result, state, execution_style):
    """Reprice the actual style without inventing a new strategy threshold."""
    style = str(execution_style or "").upper()
    style = "MAKER" if style in {"MAKER", "MAKER_TRADE_THROUGH"} else "TAKER"
    contract = dict((result or {}).get("execution_cost_contract") or {})
    if not contract:
        contract = dict(
            ((result or {}).get("edge_tier") or {}).get(
                "execution_cost_contract"
            ) or {}
        )
    reserved = dict(
        getattr(state, "canonical_reserved_context", {}) or {}
    )
    same_reservation = bool(
        int(reserved.get("opportunity_id", 0) or 0)
        == int((result or {}).get("canonical_opportunity_id", 0) or 0)
        and str(reserved.get("causal_episode_id") or "")
        == str((result or {}).get("causal_episode_id") or "")
    )
    if same_reservation and reserved.get("execution_cost_contract"):
        contract = dict(reserved["execution_cost_contract"])
    budgets = dict(contract.get("budgets_bps") or {})
    budget = _f(budgets.get(style), -1.0)
    current = estimate(
        dict(result or {}, phase="ACCEPTANCE" if style == "MAKER" else "RELEASE"),
        state,
    )
    current_total = _f(current.get("total_cost_bps"), -1.0)
    if (
        str(contract.get("version") or "")
        != "EXECUTION_COST_CONTRACT_V1_STYLE_SPECIFIC"
        or budget < 0.0
    ):
        return False, "EXECUTION_COST_CONTRACT_MISSING", {
            "execution_style": style, "current": current,
        }
    verified_cost = bool(
        contract.get("commission_verified")
        and current.get("commission_verified")
    )
    simulated_shadow_cost = bool(
        contract.get("simulation_cost_usable")
        and current.get("simulation_cost_usable")
        and shadow_commission_profile(state)
        and not bool(getattr(state, "wstrade_live_armed", False))
    )
    if not (verified_cost or simulated_shadow_cost):
        return False, "EXECUTION_COMMISSION_UNVERIFIED", {
            "execution_style": style, "budget_bps": budget, "current": current,
        }
    if str(contract.get("commission_source") or "") != str(
        current.get("commission_source") or ""
    ):
        return False, "EXECUTION_COMMISSION_SOURCE_CHANGED", {
            "execution_style": style, "budget_bps": budget, "current": current,
        }
    if current_total > budget + 1e-9:
        return False, "EXECUTION_COST_WORSE_THAN_DECISION", {
            "execution_style": style,
            "budget_bps": round(budget, 6),
            "current_cost_bps": round(current_total, 6),
            "current": current,
        }
    return True, (
        "EXECUTION_COST_CONTRACT_PASS"
        if verified_cost else "SHADOW_SIMULATED_COST_CONTRACT_PASS"
    ), {
        "execution_style": style,
        "budget_bps": round(budget, 6),
        "current_cost_bps": round(current_total, 6),
        "current": current,
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
        "version": FROZEN_COST_PLAN_VERSION,
        "execution_style": "MAKER" if is_maker else "TAKER",
        "fill_style": style or "MARKET",
        "commission_verified": bool(modeled["commission_verified"]),
        "simulation_cost_usable": bool(
            modeled.get("simulation_cost_usable")
        ),
        "commission_profile": modeled.get("commission_profile"),
        "commission_source": modeled["commission_source"],
        "commission_verification_reason": modeled.get(
            "commission_verification_reason"
        ),
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
        "exit_execution_cost_embedded_in_fill": True,
        "roundtrip_cost_bps": float(modeled["total_cost_bps"]),
        "remaining_recovery_cost_bps": round(remaining, 6),
        "ledger_fee_bps": round(
            float(modeled["entry_fee_bps"]) + float(modeled["exit_fee_bps"]),
            6,
        ),
        # Compatibility field: Guardian/Risk recover only costs not already
        # embedded in the executable entry fill.
        "total_cost_bps": round(remaining, 6),
        "minimum_net_edge_bps": float(modeled["minimum_net_edge_bps"]),
    }


def position_total_cost_bps(position, fallback_bps=18.0):
    """Cost floor used by Risk for an existing position, including slippage."""
    plan = (getattr(position, "execution_cost_plan", None)
            or getattr(position, "shadow_cost_plan", None) or {})
    try:
        value = float(plan.get("total_cost_bps", 0.0) or 0.0)
    except (AttributeError, TypeError, ValueError):
        value = 0.0
    return value if value > 0.0 else max(0.0, float(fallback_bps or 0.0))


def position_roundtrip_cost_bps(position, fallback_bps=None):
    """Return immutable decision-time round-trip cost for audit/ledger parity."""
    fallback = (
        2.0 * fallback_fee_bps_per_side()
        if fallback_bps is None else max(0.0, _f(fallback_bps))
    )
    plan = (getattr(position, "execution_cost_plan", None)
            or getattr(position, "shadow_cost_plan", None) or {})
    value = _f(plan.get("roundtrip_cost_bps"), 0.0)
    if value <= 0.0:
        value = _f(plan.get("decision_total_cost_bps"), 0.0)
    return value if value > 0.0 else fallback


def position_fee_components(position):
    """Return frozen entry/exit commission; never mix 5/9/verified defaults."""
    fallback = fallback_fee_bps_per_side()
    plan = (getattr(position, "execution_cost_plan", None)
            or getattr(position, "shadow_cost_plan", None) or {})
    entry = _f(plan.get("entry_fee_bps"), fallback)
    exit_ = _f(plan.get("exit_fee_bps"), fallback)
    return max(0.0, entry), max(0.0, exit_)
