"""Neutralize stale OI and quarantine post-gap recovery without destroying real history."""
from collections import deque
import time

VERSION = "REGIME_OI_FRESHNESS_HOOK_V3_RECOVERY_EPOCH"
MAX_OI_AGE_S = 3.0
RECOVERY_FRESH_SAMPLES = 2
RECOVERY_MASK_S = 8.5


def _f(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def install(regime_module):
    if getattr(regime_module, "_oi_freshness_hooked", False):
        return

    original = regime_module.classify

    def classify(state, side=None):
        now = time.time()
        updated = _f(getattr(state, "open_interest_updated_at", 0.0))
        age = now - updated if updated > 0.0 else 999.0
        stale = age > MAX_OI_AGE_S
        current_oi = _f(getattr(state, "open_interest", 0.0))

        was_stale = bool(getattr(state, "_tier_s_oi_was_stale", False))
        count = int(getattr(state, "_tier_s_oi_recovery_count", 0) or 0)
        last_seen = _f(getattr(state, "_tier_s_oi_last_update_seen", 0.0))
        started_at = _f(getattr(state, "_tier_s_oi_recovery_started_at", 0.0))
        baseline = _f(getattr(state, "_tier_s_oi_recovery_baseline", 0.0))

        if stale:
            state._tier_s_oi_was_stale = True
            state._tier_s_oi_recovery_count = 0
            state._tier_s_oi_last_update_seen = updated
            was_stale = True
            count = 0
        elif was_stale and updated > last_seen + 1e-6:
            if count == 0:
                started_at = now
                baseline = current_oi
                state._tier_s_oi_recovery_started_at = started_at
                state._tier_s_oi_recovery_baseline = baseline
            count += 1
            state._tier_s_oi_recovery_count = count
            state._tier_s_oi_last_update_seen = updated
            if count >= RECOVERY_FRESH_SAMPLES:
                state._tier_s_oi_was_stale = False

        recovering = (not stale) and was_stale and count < RECOVERY_FRESH_SAMPLES
        mask_active = (
            not stale
            and started_at > 0.0
            and baseline > 0.0
            and now - started_at <= RECOVERY_MASK_S
        )

        original_hist = getattr(state, "_micro_regime_hist", None)
        swapped = False
        if original_hist and (stale or recovering or mask_active):
            if stale:
                temp_hist = deque(
                    ((row[0], row[1], current_oi, row[3]) for row in original_hist),
                    maxlen=getattr(original_hist, "maxlen", 64),
                )
            else:
                temp_hist = deque(
                    (
                        (
                            row[0],
                            row[1],
                            baseline if row[0] < started_at else row[2],
                            row[3],
                        )
                        for row in original_hist
                    ),
                    maxlen=getattr(original_hist, "maxlen", 64),
                )
            state._micro_regime_hist = temp_hist
            swapped = True

        temp_hist_after = None
        try:
            report = dict(original(state, side) or {})
            if swapped:
                temp_hist_after = getattr(state, "_micro_regime_hist", None)
        finally:
            if swapped:
                state._micro_regime_hist = original_hist

        # Preserve the real history. Only mirror a newly appended timestamp from
        # the temporary calculation, using the real current OI value.
        if swapped and temp_hist_after:
            latest = temp_hist_after[-1]
            if not original_hist or latest[0] > original_hist[-1][0]:
                original_hist.append(
                    (latest[0], latest[1], current_oi, latest[3])
                )

        report["oi_fresh"] = not stale
        report["oi_age_s"] = round(max(0.0, age), 3)
        report["oi_recovery_samples"] = count if (was_stale or recovering) else 0
        report["oi_recovery_masked"] = bool(mask_active)

        if stale or recovering:
            report["oi_pct"] = 0.0
            report["oi_fast_pct"] = 0.0
            report["oi_accel_pct"] = 0.0
            report["oi_signature"] = "STALE_NEUTRAL" if stale else "RECOVERY_WARMUP"
            report["policy"] = (
                "STALE_OI_NEUTRAL_PRICE_FLOW_ACTIVE"
                if stale
                else "OI_RECOVERY_WARMUP_PRICE_FLOW_ACTIVE"
            )
            state.tier_s_micro_regime = report

        if started_at > 0.0 and now - started_at > RECOVERY_MASK_S:
            state._tier_s_oi_recovery_started_at = 0.0
            state._tier_s_oi_recovery_baseline = 0.0

        return report

    regime_module.classify = classify
    regime_module._oi_freshness_hooked = True
