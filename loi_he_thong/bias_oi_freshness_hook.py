"""Keep Bias OI freshness aligned with the 1s OI collector."""
VERSION = "BIAS_OI_FRESHNESS_HOOK_V1"
MAX_OI_AGE_S = 3.0

def install(bias_module):
    bias_module.OI_AGE = MAX_OI_AGE_S
    bias_module.BIAS_OI_FRESHNESS_POLICY = VERSION
    return MAX_OI_AGE_S
