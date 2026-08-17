"""Mainnet-only Tier-S shadow runtime.

Consumes Binance Spot/Mainnet Futures + Coinbase public data.
It can simulate a fixed 0.001 BTC position, but it never submits,
cancels, or modifies exchange orders/account settings.
"""
import asyncio
from collections import deque
import faulthandler
import json
import logging
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import time

os.environ["SMC_EXECUTION_VENUE"] = "MAINNET"
os.environ["SMC_ENABLE_TRADING"] = "false"
os.environ["SMC_MAINNET_ARMED"] = "false"
os.environ["SMC_MAINNET_EXCLUSIVE_ACCOUNT"] = "false"

import khoi_dong as app

VERSION = "MAINNET_TIER_S_SHADOW_V1"
ENTRY_POLL = 0.10
BIAS_SCOUT = 0.25
GUARD_POLL = 0.05
IDLE = 60.0
QTY_BTC = 0.001
LEVERAGE = 20
START_BALANCE_USDT = float(os.getenv("SMC_SHADOW_BALANCE_USDT", "5.4"))
FEE_BPS_PER_SIDE = float(os.getenv("SMC_SHADOW_FEE_BPS_PER_SIDE", "5.0"))
STATE_DIR = Path(os.getenv(
    "SMC_JOURNAL_DIR",
    "/home/ubuntu/.local/state/smc2026/mainnet_shadow",
))
EVENT_PATH = Path(os.getenv(
    "SMC_SHADOW_EVENTS_PATH",
    str(STATE_DIR / "events.jsonl"),
))

entry_council = app.load_module(
    "entry_council_mainnet_shadow",
    app.CURRENT_DIR / "loi_he_thong" / "entry_council_shadow.py",
)
bias_council = app.load_module(
    "bias_council_mainnet_shadow",
    app.CURRENT_DIR / "2_suy_luan_mapping" / "bias_council.py",
)
guardian_s = app.load_module(
    "guardian_s_mainnet_shadow",
    app.CURRENT_DIR / "3_thuc_thi" / "ve_si_lenh" / "guardian_s_tier.py",
)


async def _idle(*_args, **_kwargs):
    while True:
        await asyncio.sleep(IDLE)


def _disable(obj, name):
    if obj is not None and hasattr(obj, name):
        setattr(obj, name, _idle)


async def _blocked_mutation(*_args, **_kwargs):
    raise RuntimeError("MAINNET_SHADOW_BLOCKED_EXCHANGE_MUTATION")


def _block_exchange_mutations():
    api = app.api
    for name in (
        "new_order",
        "new_order_test",
        "new_algo_order",
        "cancel_order",
        "cancel_all_open_orders",
        "cancel_algo_order",
        "cancel_all_algo_orders",
        "change_position_mode",
        "change_multi_asset_mode",
        "change_margin_type",
        "change_leverage",
    ):
        if hasattr(api, name):
            setattr(api, name, _blocked_mutation)


async def _shadow_account_init():
    s = app.state
    s.balance_usdt = START_BALANCE_USDT
    s.account_ready = True
    s.execution_allowed = False
    s.execution_venue = "BINANCE_FUTURES_MAINNET_SHADOW"
    s._api_is_testnet = False
    s.mainnet_shadow = True
    s.mainnet_shadow_version = VERSION
    s.mainnet_shadow_qty_btc = QTY_BTC
    s.mainnet_shadow_leverage = LEVERAGE
    s.mainnet_shadow_balance_usdt = START_BALANCE_USDT
    s.mainnet_shadow_real_orders_blocked = True
    logging.info(
        "[MAINNET-SHADOW] public Mainnet data only; real exchange mutations blocked; "
        "qty=%.3f BTC balance_model=%.2f USDT",
        QTY_BTC,
      START_BALANCE_USDT,
    )


def _cheap_profile(klines):
    close = 0.0
    try:
        if klines:
            close = float(klines[-1][4])
    except (IndexError, TypeError, ValueError):
        pass
    if close <= 0:
        close = _spot_mid()
    return {"poc": close, "vah": close, "val": close, "lvn_zones": []}


def _spot_mid():
    s = app.state
    bid = float(getattr(s, "best_bid", 0.0) or 0.0)
    ask = float(getattr(s, "best_ask", 0.0) or 0.0)
    return (bid + ask) / 2.0 if bid > 0 and ask > bid else max(bid, ask)


def _latest_futures_price(now=None):
    now = time.time() if now is None else float(now)
    rows = getattr(app.state, "danh_sach_khop_lenh_futures", None) or ()
    try:
        row = rows[-1]
        ts = float(row.get("thoi_gian_ms", 0.0) or 0.0) / 1000.0
        px = float(row.get("gia", 0.0) or 0.0)
        if px > 0 and ts > 0 and now - ts <= 5.0:
            return px
    except (AttributeError, IndexError, TypeError, ValueError):
        pass
    return _spot_mid()


def _spot_fresh(now):
    ts = float(getattr(app.state, "thoi_gian_tick_cuoi", 0.0) or 0.0)
    return ts > 0 and now - ts <= 3.0

def _flow_volume_quorum(state, now):
    floor = max(
        0.02,
        min(0.10, 0.02 * float(getattr(state, "vol_pct90", 0.0) or 0.0)),
    )
    venues = {}

    spot_ts = float(getattr(state, "thoi_gian_dong_tien_cuoi", 0.0) or 0.0)
    spot_vol = (
        float(getattr(state, "current_cvd_buy_3s", 0.0) or 0.0)
        + float(getattr(state, "current_cvd_sell_3s", 0.0) or 0.0)
    )
    if spot_ts > 0 and now - spot_ts <= 5.0 and spot_vol >= floor:
        venues["spot"] = spot_vol

    cb_ts = float(getattr(state, "coinbase_flow_3s_ts", 0.0) or 0.0)
    cb_vol = float(getattr(state, "coinbase_volume_3s", 0.0) or 0.0)
    if cb_ts > 0 and now - cb_ts <= 5.0 and cb_vol >= floor:
        venues["coinbase"] = cb_vol

    cutoff = (now - 3.0) * 1000.0
    fut_vol = 0.0
    newest = 0.0
    for row in list(getattr(state, "danh_sach_khop_lenh_futures", ()) or ()):
        try:
            ts = float(row.get("thoi_gian_ms", 0.0) or 0.0)
            if ts < cutoff:
                continue
            fut_vol += float(row.get("khoi_luong", 0.0) or 0.0)
            newest = max(newest, ts / 1000.0)
        except (AttributeError, TypeError, ValueError):
            continue
    if newest > 0 and now - newest <= 5.0 and fut_vol >= floor:
        venues["futures"] = fut_vol

    state.entry_tier_s_volume_quality = {
        "floor_btc": floor,
        "venues": venues,
        "required": 2,
    }
    return len(venues) >= 2


def _entry_quorum_ok(result, state, now):
    if not result or result.get("decision") != "GO":
        return False
    votes = result.get("s_votes") or {}
    s1 = votes.get("S1_cross_venue_price_acceptance") or {}
    s2 = votes.get("S2_multi_venue_executed_flow") or {}
    if s1.get("status") != "PASS" or s2.get("status") != "PASS":
        return False
    return _flow_volume_quorum(state, now)


def _entry_feasibility(price):
    notional = max(0.0, float(price)) * QTY_BTC
    margin = notional / max(LEVERAGE, 1)
    roundtrip_fee_reserve = notional * (2.0 * FEE_BPS_PER_SIDE) / 10000.0
    required = margin + roundtrip_fee_reserve
    return {
        "price": price,
        "qty_btc": QTY_BTC,
        "notional_usdt": notional,
        "leverage_model": LEVERAGE,
        "initial_margin_usdt": margin,
        "roundtrip_fee_reserve_usdt": roundtrip_fee_reserve,
        "required_usdt": required,
        "balance_usdt": float(
            getattr(app.state, "mainnet_shadow_balance_usdt", START_BALANCE_USDT)
            or 0.0
        ),
        "feasible": required <= float(
            getattr(app.state, "mainnet_shadow_balance_usdt", START_BALANCE_USDT)
            or 0.0
       ),
    }


def _append_event(event, payload):
    EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.time(),
        "runtime": VERSION,
        "event": event,
        **payload,
    }
    with EVENT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _open_shadow(side, result, now):
    price = _latest_futures_price(now)
    feasibility = _entry_feasibility(price)
    app.state.mainnet_shadow_last_feasibility = feasibility
    if price <= 0 or not feasibility["feasible"]:
        app.state.mainnet_shadow_last_skip = "SHADOW_BALANCE_INSUFFICIENT"
        _append_event(
            "ENTRY_SKIPPED",
            {
                "reason": "SHADOW_BALANCE_INSUFFICIENT",
                "side": side,
                "entry": result,
                "feasibility": feasibility,
            },
        )
        return None

    pos = SimpleNamespace(
        active=True,
        side=side,
        qty=QTY_BTC,
        initial_qty=QTY_BTC,
        opened_at=now,
        position_cycle_id=f"shadow:{side}:{int(now * 1000)}",
        entry_price=price,
        execution_entry_price=price,
    )
    app.state.mainnet_shadow_position = pos
    app.state.mainnet_shadow_position_status = "OPEN"
    app.state.mainnet_shadow_last_entry = result
    _append_event(
        "ENTRY",
        {
            "side": side,
            "price": price,
            "qty_btc": QTY_BTC, 
            "entry_mode": result.get("entry_mode"),
            "phase": result.get("phase"),
            "confidence": result.get("confidence"),
            "feasibility": feasibility,
        },
    )
    logging.info(
        "[MAINNET-SHADOW] ENTRY %s %.3f BTC @ %.2f mode=%s phase=%s",
        side,
        QTY_BTC,
        price,
        result.get("entry_mode"),
        result.get("phase"),
    )
    return pos


def _close_shadow(pos, guardian_result, now):
    price = _latest_futures_price(now)
    if price <= 0:
        return
    entry = float(pos.entry_price)
    gross = (
        (price - entry) * QTY_BTC
        if pos.side == "LONG"
        else (entry - price) * QTY_BTC
    )
    fees = (
        entry * QTY_BTC * FEE_BPS_PER_SIDE / 10000.0
        + price * QTY_BTC * FEE_BPS_PER_SIDE / 10000.0
    )
    net = gross - fees
    app.state.mainnet_shadow_balance_usdt = float(
        getattr(app.state, "mainnet_shadow_balance_usdt", START_BALANCE_USDT)
        or 0.0
    ) + net
    pos.active = False
    app.state.mainnet_shadow_position_status = "FLAT"
    app.state.mainnet_shadow_last_exit = guardian_result
    _append_event(
        "EXIT",
        {
            "side": pos.side,
            "entry_price": entry,
            "exit_price": price,
            "qty_btc": QTY_BTC,
            "gross_pnl_usdt": gross,
            "fees_usdt": fees,
            "net_pnl_usdt": net,
            "balance_usdt": app.state.mainnet_shadow_balance_usdt,
            "guardian": guardian_result,
        },
    )
    logging.info(
        "[MAINNET-SHADOW] EXIT %w %.3f BTC @ %.2f net=%+.4f balance=%.4f",
        pos.side,
        QTY_BTC,
        price,
        net,
        app.state.mainnet_shadow_balance_usdt,
    )


def _apply_runtime():
    if bool(getattr(app.api, "testnet", True)):
        raise RuntimeError("MAINNET_SHADOW_REQUIRES_MAINNET_PUBLIC_ENDPOINTS")

    _block_exchange_mutations()
    app.khoi_tao_tai_khoan = _shadow_account_init

    # No exchange execution/reconcile/private journal loops in this runtime.
    _disable(app.dat_lenh, "vong_lap_thuc_thi")
    _disable(app.bao_ve_khan_cap, "vong_lap_bao_ve")
    _disable(app.tho_san_trailing, "vong_lap_trailing")
    _disable(app.dong_bo_trang_thai, "vong_lap_dong_bo")
    _disable(app.dong_bo_trang_thai, "vong_lap_doi_chieu")
    _disable(app.nhat_ky_giao_dich, "vong_lap_nhat_ky")

    # Keep Spot depth + M1 for freshness/ATR; remove legacy authority.
    _disable(getattr(app, "tai_so_lenh", None), "hung_so_lenh_futures")
    _disable(getattr(app, "tai_so_lenh", None), "hung_so_lenh_futures_execution")
    _disable(app, "vong_lap_nen_m15")
    _disable(app, "vong_lap_vi_mo_mapping")
    _disable(getattr(app, "map_gia_tick", None), "vong_lap_radar")

    app.footprint.cap_nhat_footprint = lambda *_a, **_k: None
    app.flash_flow.cap_nhat_nguong_ca_map = lambda *_a, **_k: None
    app.tri_oracle.cap_nhat_tri_oracle = lambda *_a, **_k: None
    app.POC_VAH_VAL.select_profile_klines = (
        lambda rows, *_a, **_k: list(rows[-2:]) if rows else []
    )
    app.POC_VAH_VAL.calculate_volume_profile = _cheap_profile

    s = app.state
    s.mainnet_shadow = True
    s.mainnet_shadow_version = VERSION
    s.mainnet_shadow_real_orders_blocked = True
    s.mainnet_shadow_qty_btc = QTY_BTC
    s.mainnet_shadow_balance_usdt = START_BALANCE_USDT

    old = getattr(s, "bias_price_history", None)
    if old is None or int(getattr(old, "maxlen", 0) or 0) < 128:
        s.bias_price_history = deque(list(old or ()), maxlen=256)

    fut_ring = getattr(s, "danh_sach_khop_lenh_futures", None)
    if fut_ring is None or int(getattr(fut_ring, "maxlen", 0) or 0) < 5000:
        s.danh_sach_khop_lenh_futures = deque(list(fut_ring or ()), maxlen=5000)

    logging.info(
        "[MAINNET-SHADOW] Tier-S causal runtime active; all exchange mutations blocked"
    )


async def _bias_loop():
    while True:
        try:
            s = app.state
            now = time.time()
            result = bias_council.evaluate(s, now=now)
            s.bias_state = result["bias"]
            s.bias_confidence = result["confidence"]
            s.bias_council = result
            s.bias_updated_at = now
            s.bias_version = result.get("version")
            s.macro_bias = "NEUTRAL"
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] fast bias scout failure")
        await asyncio.sleep(BIAS_SCOUT)


async def _entry_loop():
    while True:
        try:
            s = app.state
            now = time.time()
            pos = getattr(s, "mainnet_shadow_position", None)
            if pos is not None and bool(getattr(pos, "active", False)):
                await asyncio.sleep(ENTRY_POLL)
                continue
            if not _spot_fresh(now):
                s.mainnet_shadow_entry_state = "WAIT_STALE_SPOT"
                await asyncio.sleep(ENTRY_POLL)
                continue

            result = entry_council.evaluate(s, now=now)
            s.entry_shadow_council = result
            s.entry_shadow_decision = result["decision"]
            s.entry_shadow_confidence = result["confidence"]
            s.entry_shadow_phase = result.get("phase")
            s.entry_shadow_mode = result.get("entry_mode")
            s.entry_shadow_updated_at = now

            if not _entry_quorum_ok(result, s, now):
                await asyncio.sleep(ENTRY_POLL)
                continue

            side = str(result.get("side" or getattr(s, "bias_state", "ABSTAIN")).upper()
            if side not in ("LONG", "SHORT"):
                await asyncio.sleep(ENTRY_POLL)
                continue
            if side != str(getattr(s, "bias_state", "ABSTAIN")).upper():
                await asyncio.sleep(ENTRY_POLL)
                continue

            claim = (
                side,
                round(float(getattr(s, "bias_updated_at", 0.0) or 0.0), 3),
                round(float(result.get("confidence", 0.0) or 0.0), 3),
            )
            if (
                claim == getattr(s, "mainnet_shadow_entry_claim", None)
                and now - float(getattr(s, "mainnet_shadow_entry_claim_at", 0.0) or 0.0) < 1.5
            ):
                await asyncio.sleep(ENTRY_POLL)
                continue
            s.mainnet_shadow_entry_claim = claim
            s.mainnet_shadow_entry_claim_at = now
            _open_shadow(side, result, now)
      except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] entry loop failure")
            await asyncio.sleep(0.5)
        await asyncio.sleep(ENTRY_POLL)


async def _guardian_loop():
    while True:
        try:
            s = app.state
            pos = getattr(s, "mainnet_shadow_position", None)
            if pos is None or not bool(getattr(pos, "active", False):
                await asyncio.sleep(0.10)
                continue
            now = time.time()
            if not _spot_fresh(now):
                s.guardian_s_decision = "HOLD_STALE_SPOT"
                await asyncio.sleep(GUARD_POLL)
                continue
            result = guardian_s.update_state(s, pos, now=now)
            if result.get("decision") == "EXIT":
                _close_shadow(pos, result, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] guardian failure")
            await asyncio.sleep(0.25)
        await asyncio.sleep(GUARD_POLL)


async def _runtime():
    _apply_runtime()
    await asyncio.gather(
        app.main(),
        _bias_loop(),
        _entry_loop(),
        _guardian_loop(),
    )


def main():
    try:
        lock = app.acquire_runtime_lock("bot_mainnet_shadow")
    except app.DuplicateInstanceError as exc:
        logging.critical("[RUNTIME] %s", exc)
        raise SystemExit(73) from exc
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        if app.uvloop is not None:
            app.uvloop.install()
        asyncio.run(_runtime())
    except KeyboardInterrupt:
        logging.info("Mainnet Tier-S shadow runtime stopped.")
    finally:
        lock.close()


if __name__ == "__main__":
    main()
