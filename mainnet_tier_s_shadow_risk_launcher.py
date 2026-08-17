"""Mainnet shadow wrapper with Tier-S adaptive SL/TP/profit protection."""
import asyncio
import logging
import time

import mainnet_tier_s_shadow_launcher as base

risk = base.app.load_module(
    "shadow_risk_guard_runtime",
    base.app.CURRENT_DIR / "loi_he_thong" / "shadow_risk_guard.py",
)

_orig_open = base._open_shadow

def _open_shadow(side, result, now):
    pos = _orig_open(side, result, now)
    if pos is not None:
        risk.arm(pos, pos.entry_price)
        base.app.state.mainnet_shadow_risk = risk.snap(pos, pos.entry_price)
        logging.info(
            "[MAINNET-SHADOW] RISK armed side=%s entry=%.2f hard_sl=%.2f tp=%.2f",
            pos.side, pos.entry_price, pos.hard_sl, pos.tp,
        )
    return pos

async def _guardian_loop():
    while True:
        try:
            s = base.app.state
            pos = getattr(s, "mainnet_shadow_position", None)
            if pos is None or not bool(getattr(pos, "active", False)):
                await asyncio.sleep(0.10)
                continue
            now = time.time()
            if not base._spot_fresh(now):
                s.guardian_s_decision = "HOLD_STALE_SPOT"
                await asyncio.sleep(base.GUARD_POLL)
                continue

            px = base._latest_futures_price(now)
            guardian = base.guardian_s.update_state(s, pos, now=now)

            rr = risk.assess(pos, px, guardian)
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

base._open_shadow = _open_shadow
base._guardian_loop = _guardian_loop

if __name__ == "__main__":
    base.main()
