"""Neutralize stale OI for regime calculations without destroying real OI history."""
from collections import deque
import time

VERSION = "REGIME_OI_FRESHNESS_HOOK_V2_PRESERVE_HISTORY"
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

        original_hist = getattr(state, "_micro_regime_hist", None)
        swapped = False

        if stale and original_hist:
            current_oi = float(getattr(state, "open_interest", 0.0) or 0.0)
            neutral_hist = deque(
                ((row[0], row[1], current_oi, row[3]) for row in original_hist),
                maxlen=getattr(original_hist, "maxlen", 64),
            )
            state._micro_regime_hist = neutral_hist
            swapped = True

        try:
            report = dict(original(state, side) or {})
            temp_hist = getattr(state, "_micro_regime_hist", None)
        finally:
            if swapped:
                state._micro_regime_hist = original_hist

        if swapped and temp_hist:
            latest = temp_hist[-1]
            if not original_hist or latest[0] > original_hist[-1][0]:
                original_hist.append(
                    (latest[0], latest[1], float(getattr(state, "open_interest", 0.0) or 0.0), latest[3])
                )

        report["oi_fresh"] = not stale
        report["oi_age_s"] = round(max(0.0, age), 3)

        if stale:
            report["oi_pct"] = 0.0
            report["oi_fast_pct"] = 0.0
            report["oi_accel_pct"] = 0.0
            report["oi_signature"] = "STALE_NEUTRAL"
            report["policy"] = "STALE_OI_NEUTRAL_PRESERVE_HISTORY_PRICE_FLOW_ACTIVE"
            state.tier_s_micro_regime = report

        return report

    regime_module.classify = classify
    regime_module._oi_freshness_hooked = True
