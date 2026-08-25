"""Keep Bias OI freshness aligned with the dynamic 15s/5s collector.

This hook deliberately has one responsibility. Ignition Core owns frozen
Bias, refreshed OI intent, phase/precursor measurement and causal vetoes.
Execution reservation and fill lifecycle are owned by canonical_opportunity
and the active launcher. Do not monkey-patch those authorities here.
"""

VERSION = "BIAS_OI_FRESHNESS_HOOK_V4_SINGLE_RESPONSIBILITY"
MAX_OI_AGE_S = 18.0


def install(bias_module):
    bias_module.OI_AGE = MAX_OI_AGE_S
    bias_module.BIAS_OI_FRESHNESS_POLICY = VERSION
    return MAX_OI_AGE_S
