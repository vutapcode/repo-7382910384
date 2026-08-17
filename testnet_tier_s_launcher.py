"""Testnet-only Tier-S runtime.

Purpose:
- keep only sensors required by Bias/Entry/Guardian Tier-S councils;
- disable legacy/heavy signal plumbing on Testnet;
- route entry through the existing Commander transport only after Tier-S entry GO;
- suppress exchange-side conditional TP/SL on Testnet so Guardian-S owns discretionary exits.

Mainnet is intentionally not supported by this launcher.
"""
import asyncio
import faulthandler
import logging
import os
import signal
import time

# Force sandbox execution before importing the application.  This prevents the
# systemd file or an inherited shell environment from accidentally selecting Mainnet.
os.environ["SMC_EXECUTION_VENUE"] = "TESTNET"

import guardian_s_launcher as guardian_launcher
import khoi_dong as app


VERSION = "TESTNET_TIER_S_RUNTIME_V1"
ENTRY_VERSION = "ENTRY_COUNCIL_TIER_S_V1"
ENTRY_POLL_SECONDS = 0.10
IDLE_SECONDS = 60.0
RETRY_COOLDOWN_SECONDS = 1.50

PROTECTIVE_ORDER_TYPES = frozenset({
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
})


def _load_entry_council():
    return app.load_module(
        "entry_council_active_testnet",
        app.CURRENT_DIR / "loi_he_thong" / "entry_council_shadow.py",
    )


entry_council = _load_entry_council()


async def _idle_task(*_args, **_kwargs):
    while True:
        await asyncio.sleep(IDLE_SECONDS)


def _cheap_volume_profile(klines):
    """Keep the M1 ATR refresh contract without running volume-profile work."""
    close = 0.0
    try:
        if klines:
            close = float(klines[-1][4])
    except (IndexError, TypeError, ValueError):
        close = 0.0
    if close <= 0.0:
        close = (
            float(getattr(app.state, "best_bid", 0.0) or 0.0)
            + float(getattr(app.state, "best_ask", 0.0) or 0.0)
        ) / 2.0
    return {"poc": close, "vah": close, "val": close, "lvn_zones": []}


def _tier_s_score(_snapshot, _mode_info, _bias):
    """Minimal compatibility score.

    It has no authority over direction or entry.  It only lets the battle-tested
    Commander queue/idempotency plumbing carry a Tier-S-approved Testnet signal.
    """
    result = getattr(app.state, "entry_shadow_council", {}) or {}
    confidence = float(result.get("confidence", 0.0) or 0.0)
    return {
        "version": ENTRY_VERSION,
        "total": 1.0,
        "core": 1,
        "effective_core": 1.0,
        "m15_modifier": 0.0,
        "poc_modifier": 0.0,
        "shark": 0,
        "detail": ["TIER_S_ENTRY_COUNCIL_GO"],
        "event_ids": [],
        "advisory": {"tier_s_entry": result},
        "evidence_quality": {},
        "score": confidence * 100.0,
        "final_score": confidence * 100.0,
    }


def _apply_lightweight_testnet_runtime():
    if not bool(getattr(app.api, "testnet", False)):
        raise RuntimeError("TESTNET_TIER_S_RUNTIME_REFUSES_MAINNET")

    state = app.state
    state.testnet_tier_s_runtime = VERSION
    state.testnet_guardian_only = True

    # Heavy/easy-to-manipulate signal sources: physically stop their runtime loops.
    app.tai_so_lenh.hung_so_lenh_futures = _idle_task
    app.tai_so_lenh.hung_so_lenh_futures_execution = _idle_task
    app.vong_lap_so_lenh = _idle_task
    app.tai_nen_live.hung_nen_live_futures = _idle_task
    app.vong_lap_nen_live = _idle_task
    app.vong_lap_nen_m15 = _idle_task
    app.vong_lap_vi_mo_mapping = _idle_task
    app.map_gia_tick.vong_lap_radar = _idle_task
    app.tho_san_trailing.vong_lap_trailing = _idle_task
    app.bao_ve_khan_cap.vong_lap_bao_ve = _idle_task

    # Keep Spot aggTrade because Tier-S flow needs it, but remove legacy per-trade work.
    app.footprint.cap_nhat_footprint = lambda *_a, **_k: None
    app.flash_flow.cap_nhat_nguong_ca_map = lambda *_a, **_k: None
    app.tri_oracle.cap_nhat_tri_oracle = lambda *_a, **_k: None

    # M1 REST remains only to refresh ATR.  POC/VAH/VAL become inert placeholders.
    app.POC_VAH_VAL.select_profile_klines = lambda rows, *_a, **_k: list(rows[-2:]) if rows else []
    app.POC_VAH_VAL.calculate_volume_profile = _cheap_volume_profile

    # Disable legacy Commander authority.  The old function is reused only as a
    # transport/idempotency layer after the new council already returned GO.
    commander = app.chi_huy_truong
    commander.cham_diem_mod.cham_diem = _tier_s_score
    commander._score_allows = lambda *_a, **_k: True
    commander._weak_gap_requires_reaction = lambda *_a, **_k: False
    commander._has_momentum_reclaim = lambda *_a, **_k: True
    commander._continuous_enabled = lambda: False
    commander._watch_enabled = lambda: False
    commander.kiem_duyet_veto.kiem_tra_veto = lambda *_a, **_k: (False, None)

    # No conditional TP/SL reaches Binance Testnet in this mode.  MARKET exits,
    # including Guardian-S exits, continue to use the real Testnet API.
    original_new_order = app.api.new_order
    original_new_algo_order = app.api.new_algo_order

    async def testnet_new_order(symbol, side, type, quantity=None, **kwargs):
        kind = str(type or "").upper()
        if kind in PROTECTIVE_ORDER_TYPES:
            state.testnet_last_suppressed_protective = {
                "kind": kind,
                "symbol": symbol,
                "side": side,
                "ts": time.time(),
            }
            logging.info("[TESTNET-TIER-S] suppress conditional order type=%s", kind)
            return {
                "orderId": 0,
                "clientOrderId": "guardian-only-testnet",
                "status": "SKIPPED_GUARDIAN_ONLY",
                "type": kind,
            }, 200
        return await original_new_order(symbol, side, type, quantity, **kwargs)

    async def testnet_new_algo_order(**params):
        state.testnet_last_suppressed_protective = {
            "kind": str(params.get("type") or params.get("orderType") or "ALGO").upper(),
            "symbol": params.get("symbol"),
            "side": params.get("side"),
            "ts": time.time(),
        }
        logging.info("[TESTNET-TIER-S] suppress conditional algo order")
        return {
            "algoId": 0,
            "clientAlgoId": "guardian-only-testnet",
            "status": "SKIPPED_GUARDIAN_ONLY",
        }, 200

    app.api.new_order = testnet_new_order
    app.api.new_algo_order = testnet_new_algo_order

    logging.info(
        "[TESTNET-TIER-S] lightweight runtime active: depth/structure/POC/footprint/"
        "legacy-radar/trailing/legacy-guardian disabled"
    )


def _entry_setup(side, now):
    state = app.state
    bias_ts = float(getattr(state, "bias_updated_at", 0.0) or 0.0)
    generation = int(max(bias_ts, now) * 10.0)
    setup_id = f"tier-s:{side}:{generation}"
    bid = float(getattr(state, "best_bid", 0.0) or 0.0)
    ask = float(getattr(state, "best_ask", 0.0) or 0.0)
    zone = (bid + ask) / 2.0 if bid > 0.0 and ask > bid else max(bid, ask)
    return {
        "setup_id": setup_id,
        "semantic_key": setup_id,
        "opportunity_id": setup_id,
        "opportunity_event_ids": [],
        "generation": generation,
        "state": "ARMED_WINDOW",
        "mode": "TIER-S",
        "bias": side,
        "zone": zone,
        "kind": "TIER_S",
        "activation_reason": "TIER_S_ENTRY_COUNCIL_GO",
        "entry_style": "MARKET",
        "evaluation_count": 0,
        "score_count": 0,
        "veto_count": 0,
        "core_reject_count": 0,
        "max_core": 0,
        "max_shark": 0,
    }


async def _entry_loop():
    commander = app.chi_huy_truong
    original_commander = commander.phan_tich_va_ra_lenh

    while True:
        try:
            state = app.state
            if (
                not bool(getattr(state, "_api_is_testnet", False))
                or not bool(getattr(state, "system_ready", False))
                or not bool(getattr(state, "trading_enabled", False))
                or bool(getattr(state, "co_lenh_mo", False))
                or bool(getattr(state, "execution_in_flight", False))
            ):
                await asyncio.sleep(ENTRY_POLL_SECONDS)
                continue

            now = time.time()
            result = entry_council.update_state(state, now=now)
            if not result or result.get("decision") != "GO":
                await asyncio.sleep(ENTRY_POLL_SECONDS)
                continue

            side = str(result.get("side") or getattr(state, "bias_state", "ABSTAIN")).upper()
            if side not in ("LONG", "SHORT"):
                await asyncio.sleep(ENTRY_POLL_SECONDS)
                continue

            if side != str(getattr(state, "bias_state", "ABSTAIN")).upper():
                await asyncio.sleep(ENTRY_POLL_SECONDS)
                continue

            claim_key = (
                side,
                round(float(getattr(state, "bias_updated_at", 0.0) or 0.0), 3),
                round(float(result.get("confidence", 0.0) or 0.0), 3),
            )
            last_key = getattr(state, "tier_s_entry_last_claim_key", None)
            last_at = float(getattr(state, "tier_s_entry_last_claim_at", 0.0) or 0.0)
            if claim_key == last_key and now - last_at < RETRY_COOLDOWN_SECONDS:
                await asyncio.sleep(ENTRY_POLL_SECONDS)
                continue

            setup = _entry_setup(side, now)
            state.tier_s_active_setup = setup
            state.tier_s_entry_last_claim_key = claim_key
            state.tier_s_entry_last_claim_at = now

            signal_payload = original_commander(
                state,
                {"modes": ["TIER-S"], "mode": "TIER-S"},
                "TIER-S",
                side,
                setup=setup,
            )
            if signal_payload is not None:
                state.tier_s_last_entry_signal = signal_payload
                state.tier_s_last_entry_signal_at = now

            await asyncio.sleep(ENTRY_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[TESTNET-TIER-S] entry loop failure")
            await asyncio.sleep(0.50)


async def _runtime():
    _apply_lightweight_testnet_runtime()
    await asyncio.gather(
        guardian_launcher._runtime(),
        _entry_loop(),
    )


def main():
    # Mirror the production singleton/fault-handler contract.
    try:
        runtime_lock = app.acquire_runtime_lock("bot")
    except app.DuplicateInstanceError as exc:
        logging.critical("[RUNTIME] %s", exc)
        raise SystemExit(73) from exc

    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        if app.uvloop is not None:
            app.uvloop.install()
        asyncio.run(_runtime())
    except KeyboardInterrupt:
        logging.info("Testnet Tier-S runtime stopped.")
    finally:
        runtime_lock.close()


if __name__ == "__main__":
    main()
