"""Tier-S Testnet runtime V2: minimal authority, smooth plumbing."""
import asyncio
from collections import deque
import faulthandler
import logging
import os
import signal
import time

os.environ["SMC_EXECUTION_VENUE"] = "TESTNET"

import khoi_dong as app

VERSION = "TESTNET_TIER_S_RUNTIME_V2"
ENTRY_POLL = 0.10
BIAS_SCOUT = 0.25
GUARD_POLL = 0.05
IDLE = 60.0
RETRY = 1.5

entry_council = app.load_module(
    "entry_council_active_testnet",
    app.CURRENT_DIR / "loi_he_thong" / "entry_council_shadow.py",
)
bias_council = app.load_module(
    "bias_council_fast_testnet",
    app.CURRENT_DIR / "2_suy_luan_mapping" / "bias_council.py",
)
guardian_s = app.load_module(
    "guardian_s_tier_testnet",
    app.CURRENT_DIR / "3_thuc_thi" / "ve_si_lenh" / "guardian_s_tier.py",
)

async def _idle(*_a, **_k):
    while True:
        await asyncio.sleep(IDLE)

def _disable(obj, name):
    if obj is not None and hasattr(obj, name):
        setattr(obj, name, _idle)

def _cheap_profile(klines):
    close=0.0
    try:
        if klines: close=float(klines[-1][4])
    except (IndexError,TypeError,ValueError):
        pass
    if close<=0:
        close=(float(getattr(app.state,"best_bid",0.0) or 0.0)+float(getattr(app.state,"best_ask",0.0) or 0.0))/2.0
    return {"poc":close,"vah":close,"val":close,"lvn_zones":[]}

def _entry_score(_snapshot,_mode,_bias):
    r=getattr(app.state,"entry_shadow_council",{}) or {}
    c=float(r.get("confidence",0.0) or 0.0)
    return {"version":"TIER_S_TRANSPORT_ONLY","total":1.0,"core":1,"effective_core":1.0,
            "m15_modifier":0.0,"poc_modifier":0.0,"shark":0,
            "detail":["TIER_S_ENTRY_GO"],"event_ids":[],
            "advisory":{"tier_s_entry":r},"evidence_quality":{},
            "score":c*100.0,"final_score":c*100.0}

def _flow_volume_quorum(state, now):
    floor=max(0.02,min(0.10,0.02*float(getattr(state,"vol_pct90",0.0) or 0.0)))
    vols=[]
    # Spot 3s rolling/tumbling accumulator
    spot_ts=float(getattr(state,"thoi_gian_dong_tien_cuoi",0.0) or 0.0)
    spot=float(getattr(state,"current_cvd_buy_3s",0.0) or 0.0)+float(getattr(state,"current_cvd_sell_3s",0.0) or 0.0)
    if spot_ts>0 and now-spot_ts<=5.0 and spot>=floor: vols.append(("spot",spot))
    # Coinbase true 3s window
    cb_ts=float(getattr(state,"coinbase_flow_3s_ts",0.0) or 0.0)
    cb=float(getattr(state,"coinbase_volume_3s",0.0) or 0.0)
    if cb_ts>0 and now-cb_ts<=5.0 and cb>=floor: vols.append(("coinbase",cb))
    # Futures true 3s window from available ring
    cutoff=(now-3.0)*1000.0
    fut=0.0; newest=0.0
    for row in list(getattr(state,"danh_sach_khop_lenh_futures",()) or ()):
        try:
            ts=float(row.get("thoi_gian_ms",0.0) or 0.0)
            if ts<cutoff: continue
            fut+=float(row.get("khoi_luong",0.0) or 0.0)
            newest=max(newest,ts/1000.0)
        except (AttributeError,TypeError,ValueError):
            continue
    if newest>0 and now-newest<=5.0 and fut>=floor: vols.append(("futures",fut))
    return len(vols)>=2, {"floor_btc":floor,"venues":dict(vols)}

def _entry_quorum_ok(result, state, now):
    if not result or result.get("decision")!="GO":
        return False
    votes=result.get("s_votes") or {}
    s1=votes.get("S1_cross_venue_price_acceptance") or {}
    s2=votes.get("S2_multi_venue_executed_flow") or {}
    if s1.get("status")!="PASS" or s2.get("status")!="PASS":
        return False
    ok,meta=_flow_volume_quorum(state,now)
    state.entry_tier_s_volume_quality=meta
    return ok

def _apply_runtime():
    if not bool(getattr(app.api,"testnet",False)):
        raise RuntimeError("TIER_S_TESTNET_RUNTIME_REFUSES_MAINNET")
    s=app.state
    s.testnet_tier_s_runtime=VERSION
    s.testnet_guardian_only=True

    # Keep Spot depth + M1 kline alive ONLY for watchdog readiness and ATR.
    # Kill expensive/legacy authority paths.
    _disable(getattr(app,"tai_so_lenh",None),"hung_so_lenh_futures")
    _disable(getattr(app,"tai_so_lenh",None),"hung_so_lenh_futures_execution")
    _disable(app,"vong_lap_nen_m15")
    _disable(app,"vong_lap_vi_mo_mapping")
    _disable(getattr(app,"map_gia_tick",None),"vong_lap_radar")
    _disable(getattr(app,"tho_san_trailing",None),"vong_lap_trailing")
    _disable(getattr(app,"bao_ve_khan_cap",None),"vong_lap_bao_ve")

    # Keep raw Spot aggTrade, disable derived legacy per-trade work.
    app.footprint.cap_nhat_footprint=lambda *_a,**_k: None
    app.flash_flow.cap_nhat_nguong_ca_map=lambda *_a,**_k: None
    app.tri_oracle.cap_nhat_tri_oracle=lambda *_a,**_k: None

    # M1 kline stays fresh, but profile/BOS/POC authority is neutralized.
    app.POC_VAH_VAL.select_profile_klines=lambda rows,*_a,**_k: list(rows[-2:]) if rows else []
    app.POC_VAH_VAL.calculate_volume_profile=_cheap_profile

    # Commander is transport/idempotency only after Tier-S GO.
    cmd=app.chi_huy_truong
    cmd.cham_diem_mod.cham_diem=_entry_score
    cmd._score_allows=lambda *_a,**_k: True
    cmd._weak_gap_requires_reaction=lambda *_a,**_k: False
    cmd._has_momentum_reclaim=lambda *_a,**_k: True
    cmd._continuous_enabled=lambda: False
    cmd._watch_enabled=lambda: False
    cmd.kiem_duyet_veto.kiem_tra_veto=lambda *_a,**_k:(False,None)

    # TP/trailing are disabled. STOP/STOP_MARKET remain as distant exchange fallback
    # so reconciliation stays healthy; Guardian-S owns all discretionary exits.
    orig_new=app.api.new_order
    orig_algo=app.api.new_algo_order
    async def new_order(symbol,side,type,quantity=None,**kw):
        kind=str(type or "").upper()
        if kind in {"TAKE_PROFIT","TAKE_PROFIT_MARKET","TRAILING_STOP_MARKET"}:
            return {"orderId":0,"clientOrderId":"tier-s-testnet-skip","status":"SKIPPED_TIER_S","type":kind},200
        return await orig_new(symbol,side,type,quantity,**kw)
    async def new_algo_order(**params):
        kind=str(params.get("type") or params.get("orderType") or "").upper()
        if kind in {"TAKE_PROFIT","TAKE_PROFIT_MARKET","TRAILING_STOP_MARKET"}:
            return {"algoId":0,"clientAlgoId":"tier-s-testnet-skip","status":"SKIPPED_TIER_S"},200
        return await orig_algo(**params)
    app.api.new_order=new_order
    app.api.new_algo_order=new_algo_order

    # 15s bias history must survive a 250ms scout cadence.
    old=getattr(s,"bias_price_history",None)
    if old is None or getattr(old,"maxlen",0) is None or int(old.maxlen or 0)<128:
        s.bias_price_history=deque(list(old or ()),maxlen=256)
    fut_ring=getattr(s,"danh_sach_khop_lenh_futures",None)
    if fut_ring is None or getattr(fut_ring,"maxlen",0) is None or int(fut_ring.maxlen or 0)<5000:
        s.danh_sach_khop_lenh_futures=deque(list(fut_ring or ()),maxlen=5000)

    logging.info("[TIER-S] runtime V2 active: legacy authority off; Spot depth/M1 retained only for readiness/ATR")

async def _bias_loop():
    while True:
        try:
            s=app.state; now=time.time()
            # no REST here: re-evaluate only from already collected websocket + latest OI
            r=bias_council.evaluate(s,now=now)
            s.bias_state=r["bias"]; s.bias_confidence=r["confidence"]; s.bias_council=r
            s.bias_updated_at=now; s.bias_version=r.get("version"); s.macro_bias="NEUTRAL"
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[TIER-S] fast bias scout failure")
        await asyncio.sleep(BIAS_SCOUT)

def _setup(side,now):
    s=app.state; gen=int(max(float(getattr(s,"bias_updated_at",0.0) or 0.0),now)*10)
    sid=f"tier-s:{side}:{gen}"
    bid=float(getattr(s,"best_bid",0.0) or 0.0); ask=float(getattr(s,"best_ask",0.0) or 0.0)
    zone=(bid+ask)/2.0 if bid>0 and ask>bid else max(bid,ask)
    return {"setup_id":sid,"semantic_key":sid,"opportunity_id":sid,"opportunity_event_ids":[],
            "generation":gen,"state":"ARMED_WINDOW","mode":"TIER-S","bias":side,"zone":zone,
            "kind":"TIER_S","activation_reason":"TIER_S_ENTRY_COUNCIL_GO","entry_style":"MARKET",
            "evaluation_count":0,"score_count":0,"veto_count":0,"core_reject_count":0,"max_core":0,"max_shark":0}

async def _entry_loop():
    commander=app.chi_huy_truong
    transport=commander.phan_tich_va_ra_lenh
    while True:
        try:
            s=app.state
            if (not bool(getattr(s,"_api_is_testnet",False)) or
                not bool(getattr(s,"system_ready",False)) or
                not bool(getattr(s,"trading_enabled",False)) or
                bool(getattr(s,"co_lenh_mo",False)) or
                bool(getattr(s,"execution_in_flight",False))):
                await asyncio.sleep(ENTRY_POLL); continue
            now=time.time()
            result=entry_council.update_state(s,now=now)
            if not _entry_quorum_ok(result,s,now):
                await asyncio.sleep(ENTRY_POLL); continue
            side=str(result.get("side") or getattr(s,"bias_state","ABSTAIN")).upper()
            if side not in ("LONG","SHORT") or side!=str(getattr(s,"bias_state","ABSTAIN")).upper():
                await asyncio.sleep(ENTRY_POLL); continue
            claim=(side,round(float(getattr(s,"bias_updated_at",0.0) or 0.0),3),round(float(result.get("confidence",0.0) or 0.0),3))
            if claim==getattr(s,"tier_s_entry_claim",None) and now-float(getattr(s,"tier_s_entry_claim_at",0.0) or 0.0)<RETRY:
                await asyncio.sleep(ENTRY_POLL); continue
            s.tier_s_entry_claim=claim; s.tier_s_entry_claim_at=now
            setup=_setup(side,now); s.tier_s_active_setup=setup
            payload=transport(s,{"modes":["TIER-S"],"mode":"TIER-S"},"TIER-S",side,setup=setup)
            if payload is not None:
                s.tier_s_last_entry_signal=payload; s.tier_s_last_entry_signal_at=now
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[TIER-S] entry loop failure")
            await asyncio.sleep(.5)
        await asyncio.sleep(ENTRY_POLL)

def _spot_fresh(state,now):
    ts=float(getattr(state,"thoi_gian_tick_cuoi",0.0) or 0.0)
    return ts>0 and now-ts<=3.0

async def _guardian_loop():
    close=app.bao_ve_khan_cap.close_position
    while True:
        try:
            s=app.state; pos=getattr(s,"vi_the_hien_tai",None)
            active=bool(pos is not None and getattr(pos,"active",False))
            busy=bool(getattr(s,"dang_xu_ly_dong_lenh",False) or getattr(s,"pending_close",None))
            now=time.time()
            if not active or busy:
                await asyncio.sleep(.10); continue
            if not _spot_fresh(s,now):
                s.guardian_s_decision="HOLD_STALE_SPOT"
                await asyncio.sleep(GUARD_POLL); continue
            r=guardian_s.update_state(s,pos,now=now)
            if r.get("decision")=="EXIT":
                await close(app.api,"BTCUSDT",pos.side,float(pos.qty),s,"TIER_S_CAUSAL_EXIT")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[TIER-S] Guardian failure; exchange hard stop remains fallback")
            await asyncio.sleep(.25)
        await asyncio.sleep(GUARD_POLL)

async def _runtime():
    _apply_runtime()
    await asyncio.gather(app.main(),_bias_loop(),_entry_loop(),_guardian_loop())

def main():
    try:
        lock=app.acquire_runtime_lock("bot")
    except app.DuplicateInstanceError as exc:
        logging.critical("[RUNTIME] %s",exc); raise SystemExit(73) from exc
    try:
        faulthandler.register(signal.SIGUSR1,all_threads=True)
        if app.uvloop is not None: app.uvloop.install()
        asyncio.run(_runtime())
    except KeyboardInterrupt:
        logging.info("Testnet Tier-S runtime stopped.")
    finally:
        lock.close()

if __name__=="__main__":
    main()
