"""Neutralize only the OI contribution to regime classification when OI is stale."""
from collections import deque
import time

VERSION = "REGIME_OI_FRESHNESS_HOOK_V1"
MAX_OI_AGE_S = 3.0


def install(regime_module):
    if getattr(regime_module, "_oi_freshness_hooked", False):
        return

    original = regime_module.classify

    def classify(state, side=None):
        now = time.time()
        updated = float(getattr(state, "open_interest_updated_at", 0.0) or 0.0)
        age = now - updated if updated > 0.0 else 999.0
        stale = age > MAX_OI_AGE_S

        if stale:
            hist = getattr(state, "_micro_regime_hist", None)
            if hist:
                current_oi = float(getattr(state, "open_interest", 0.0) or 0.0)
                normalized = deque(
                    ((row[0], row[1], current_oi, row[3]) for row in hist),
                    maxlen=getattr(hist, "maxlen", 64),
                )
                state._micro_regime_hist = normalized

        report = dict(original(state, side) or {})
        report["oi_fresh"] = not stale
        report["oi_age_s"] = round(max(0.0, age), 3)

        if stale:
            report["oi_pct"] = 0.0
            report["oi_fast_pct"] = 0.0
            report["oi_accel_pct"] = 0.0
            report["oi_signature"] = "STALE_NEUTRAL"
            report["policy"] = "STALE_OI_NEUTRAL_PRICE_FLOW_REMAIN_ACTIVE"
            state.tier_s_micro_regime = report

        return report

    regime_module.classify = classify
    regime_module._oi_freshness_hooked = True
