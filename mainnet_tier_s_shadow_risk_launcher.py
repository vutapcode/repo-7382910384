"""Mainnet shadow wrapper with Tier-S adaptive SL/TP/profit protection and entry edge gating."""
import asyncio
import logging
import time

import mainnet_tier_s_shadow_launcher as base

risk = base.app.load_module(
    "shadow_risk_guard_runtime",
    base.app.CURRENT_DIR / "loi_he_thong" / "shadow_risk_guard.py",
)
edge = base.app.load_module(
    "entry_edge_tier_runtime",
    base.app.CURRENT_DIR / "loi_he_thong" / "entry_edge_tier.py",
)

_orig_open = base._open_shadow

def _entry_quorum_ok(result, state, now):
    allowed, report = edge.authorize(result, state)
    state.entry_edge_tier = report
    state.entry_edge_class = report.get("edge_class")
    state.entry_edge_cost_ok = report.get("cost_ok")
    state.entry_edge_updated_at = now
    if not allowed:
        return False
    if report.get("entry_mode") == "FAST":
        # FAST deliberately permits one strong material flow venue, but never stale/tiny flow.
        return base._flow_volume_quorum(state, now, required=1)
    # NORMAL keeps the stricter two-venue material/fresh flow floor.
    return base._flow_volume_quorum(state, now, required=2)

def _open_shadow(side, result, now):
    state = base.app.state
    report = getattr(state, "entry_edge_tier", None) or edge.classify(result, state)
    result = dict(result)
    result["edge_tier"] = report
    pos = _orig_open(side, result, now)
    if pos is not None:
        risk.arm(pos, pos.entry_price)
        state.mainnet_shadow_risk = risk.snap(pos, pos.entry_price)
        state.mainnet_shadow_entry_edge = report
        logging.info(
            "[MAINNET-SHADOW] ENTRY edge=%s costx=%.2f fast=%s",
            report.get("edge_class"),
            float(report.get("cost_multiple_model") or 0.0),
            bool(report.get("fast_contract_ok")),
        )
    return pos


async def _bias_loop():
    """Run the direction-only Bias Council through update_state so hysteresis is live."""
    while True:
        try:
            s = base.app.state
            result = base.bias_council.update_state(s, now=time.time())
            s.bias_council = result
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] causal Tier-S bias loop failure")
            await asyncio.sleep(0.50)
        await asyncio.sleep(base.BIAS_SCOUT)

async def _guardian_loop():
    while True:
        try:
            s = base.app.state
            pos = getattr(s, "mainnet_shadow_position", None)
            if pos is None or not bool(getattr(pos, "active", False):
                await asyncio.sleep(0.10)
                continue
            now = time.time()
            if not base._spot_fresh(now):
                s.guardian_s_decision = "HOLD_STALE_SPOT"
                await asyncio.sleep(base.GUARD_POLL)
                continue

            px = base._latest_futures_price(now)
            guardian = base.guardian_s.update_state(s, pos, now=now)

            rr = risk.assess(pos, px, guardian, market_state=s, now=now)
            s.mainnet_shadow_risk = rr
            s.mainnet_shadow_tier_mode = rr.get("tier_mode")
            s.mainnet_shadow_tier_supportive = rr.get("supportive_count", 0)
            s.mainnet_shadow_tier_adverse = rr.get("adverse_count", 0)

            if rr.get("decision") == "EXIT":
                base._close_shadow(
                    pos,
                    {"decision": "EXIT", "reason": rr["reason"], "risk": rr, "guardian": guardian},
                    now,
                )
                await asyncio.sleep(base.GUARD_POLL)
                continue

            if guardian.get("decision") == "EXIT" and risk.guardian_ok(guardian):
                base._close_shadow(pos, guardian, now)
            elif guardian.get("decision") == "EXIT":
                s.guardian_s_decision = "WATCH_CAUSAL_GATE"
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("[MAINNET-SHADOW] Tier-S protected guardian failure")
            await asyncio.sleep(0.25)
        await asyncio.sleep(base.GUARD_POLL)

base._entry_quorum_ok = _entry_quorum_ok
base._bias_loop = _bias_loop
base._open_shadow = _open_shadow
base._guardian_loop = _guardian_loop

if __name__ == "__main__":
    base.main()
