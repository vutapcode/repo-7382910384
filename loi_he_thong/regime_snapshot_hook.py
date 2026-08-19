"""Short-lived regime snapshot so threshold and edge share one decision context."""
import time

VERSION = "REGIME_SNAPSHOT_HOOK_V1"
SNAPSHOT_TTL_S = 0.075


def install(regime_module):
    if getattr(regime_module, "_snapshot_hooked", False):
        return

    original = regime_module.classify

    def classify(state, side=None):
        now = time.monotonic()
        key = str(side or getattr(state, "bias_state", "") or "").upper()
        cached = getattr(state, "_tier_s_regime_snapshot", None)

        if isinstance(cached, dict):
            age = now - float(cached.get("mono", 0.0) or 0.0)
            if age <= SNAPSHOT_TTL_S and cached.get("side") == key:
                report = dict(cached.get("report") or {})
                report["snapshot_reused"] = True
                return report

        report = dict(original(state, side) or {})
        report["snapshot_reused"] = False
        state._tier_s_regime_snapshot = {
            "mono": now,
            "side": key,
            "report": dict(report),
        }
        return report

    regime_module.classify = classify
    regime_module._snapshot_hooked = True
