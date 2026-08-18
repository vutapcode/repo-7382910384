"""Install bounded regime adaptation onto Entry Council price threshold."""
from loi_he_thong import microstructure_regime

VERSION = "ENTRY_REGIME_THRESHOLD_HOOK_V1"

def install(entry_council_module):
    if getattr(entry_council_module, "_tier_s_regime_threshold_hooked", False):
        return
    original = entry_council_module._threshold_bps

    def adaptive_threshold(state, spot):
        base = float(original(state, spot))
        side = str(getattr(state, "bias_state", "") or "").upper()
        report = microstructure_regime.classify(state, side)
        factor = float(report.get("price_factor", 1.0) or 1.0)
        # Keep adaptation bounded: small discount in genuine expansion, tighter in chop/perp-led risk.
        factor = max(0.88, min(1.18, factor))
        adapted = base * factor
        return max(0.45, min(2.75, adapted))

    entry_council_module._threshold_bps = adaptive_threshold
    entry_council_module._tier_s_regime_threshold_hooked = True
