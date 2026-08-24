"""Keep Bias OI freshness aligned with the dynamic 15s/5s collector."""
VERSION = "BIAS_OI_FRESHNESS_HOOK_V2_DYNAMIC_15S_5S"
MAX_OI_AGE_S = 18.0

def install(bias_module):
    bias_module.OI_AGE = MAX_OI_AGE_S
    bias_module.BIAS_OI_FRESHNESS_POLICY = VERSION
    return MAX_OI_AGE_S
