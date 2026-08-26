"""UTC+7 daily PnL audit with an optional breaker.

Shadow uses ``enforce=False`` so losing test samples keep running.  The default
remains enforcing for callers/tests that explicitly use this helper as a risk
breaker; real-money authority lives in ``wstrade_live_execution.py``.
"""

from datetime import datetime, timedelta, timezone
import time


VN_TZ = timezone(timedelta(hours=7))


def day_start_ms(now=None):
    wall = time.time() if now is None else float(now)
    local = datetime.fromtimestamp(wall, VN_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _audit_only(state):
    state.mainnet_shadow_daily_locked = False
    state.mainnet_shadow_daily_lock_reason = "SHADOW_AUDIT_ONLY"
    state.mainnet_shadow_daily_lock_at = 0.0


def initialize(state, now=None, restored=False, checkpoint_ts=0.0, limit=0.60,
               enforce=True):
    """Initialize or migrate a durable day bucket conservatively.

    Old checkpoints did not persist a daily bucket.  If such a checkpoint was
    written during the current UTC+7 day, its cumulative realized PnL is used as
    today's PnL so a restart cannot erase a breached breaker.
    """
    wall = time.time() if now is None else float(now)
    today = day_start_ms(wall)
    stored_day = int(getattr(state, "mainnet_shadow_day_start_ms", 0) or 0)
    if stored_day != today:
        checkpoint_today = (
            bool(restored)
            and float(checkpoint_ts or 0.0) > 0.0
            and day_start_ms(checkpoint_ts) == today
        )
        state.mainnet_shadow_day_start_ms = today
        state.mainnet_shadow_day_realized_pnl = (
            float(getattr(state, "mainnet_shadow_realized_pnl", 0.0) or 0.0)
            if checkpoint_today
            else 0.0
        )
        state.mainnet_shadow_daily_locked = False
    realized = float(
        getattr(state, "mainnet_shadow_day_realized_pnl", 0.0) or 0.0
    )
    if enforce and realized <= -abs(float(limit)):
        state.mainnet_shadow_daily_locked = True
    elif not enforce:
        _audit_only(state)
    return today


def roll_day(state, now=None):
    wall = time.time() if now is None else float(now)
    today = day_start_ms(wall)
    if int(getattr(state, "mainnet_shadow_day_start_ms", 0) or 0) != today:
        state.mainnet_shadow_day_start_ms = today
        state.mainnet_shadow_day_realized_pnl = 0.0
        state.mainnet_shadow_daily_locked = False
        state.mainnet_shadow_daily_lock_reason = None
        state.mainnet_shadow_daily_lock_at = 0.0
    return today


def report(state, position=None, bid=0.0, ask=0.0, fee_bps_per_side=0.0,
           limit=0.60, now=None, enforce=True):
    wall = time.time() if now is None else float(now)
    today = roll_day(state, wall)
    realized = float(
        getattr(state, "mainnet_shadow_day_realized_pnl", 0.0) or 0.0
    )
    open_net = 0.0
    if position is not None and bool(getattr(position, "active", False)):
        side = str(getattr(position, "side", "") or "").upper()
        entry = float(getattr(position, "entry_price", 0.0) or 0.0)
        qty = abs(float(getattr(position, "qty", 0.0) or 0.0))
        exit_price = float(bid if side == "LONG" else ask)
        if side in ("LONG", "SHORT") and entry > 0.0 and exit_price > 0.0 and qty > 0.0:
            gross = (
                (exit_price - entry) * qty
                if side == "LONG"
                else (entry - exit_price) * qty
            )
            fees = (
                (entry + exit_price) * qty
                * max(0.0, float(fee_bps_per_side)) / 10000.0
            )
            open_net = gross - fees
    equity_pnl = realized + open_net
    would_lock = equity_pnl <= -abs(float(limit))
    locked = bool(getattr(state, "mainnet_shadow_daily_locked", False)) if enforce else False
    if enforce and would_lock:
        locked = True
        state.mainnet_shadow_daily_locked = True
        state.mainnet_shadow_daily_lock_reason = "DAILY_EQUITY_LOSS_BREAKER"
        state.mainnet_shadow_daily_lock_at = wall
    elif not enforce:
        _audit_only(state)
    return {
        "day_start_ms": today,
        "realized_pnl_usdt": realized,
        "open_net_pnl_usdt": open_net,
        "equity_pnl_usdt": equity_pnl,
        "limit_usdt": abs(float(limit)),
        "locked": locked,
        "would_lock_if_live": would_lock,
        "enforced": bool(enforce),
    }


def record_close(state, net_pnl, now=None, limit=0.60, enforce=True):
    wall = time.time() if now is None else float(now)
    roll_day(state, wall)
    state.mainnet_shadow_day_realized_pnl = float(
        getattr(state, "mainnet_shadow_day_realized_pnl", 0.0) or 0.0
    ) + float(net_pnl or 0.0)
    if enforce and state.mainnet_shadow_day_realized_pnl <= -abs(float(limit)):
        state.mainnet_shadow_daily_locked = True
        state.mainnet_shadow_daily_lock_reason = "DAILY_REALIZED_LOSS_BREAKER"
        state.mainnet_shadow_daily_lock_at = wall
    elif not enforce:
        _audit_only(state)
    return float(state.mainnet_shadow_day_realized_pnl)
