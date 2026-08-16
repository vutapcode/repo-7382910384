"""Versioned strategy-profile switches with fail-safe defaults."""

import os


AUG13_EARLY_HYBRID_V1 = 'AUG13_EARLY_HYBRID_V1'
AUG13_ADAPTIVE_OVERFIT_V1 = 'AUG13_ADAPTIVE_OVERFIT_V1'
DEFAULT_PROFILE = 'CURRENT_MAINNET_V1'


def current_profile():
    return str(
        os.getenv('SMC_STRATEGY_PROFILE', DEFAULT_PROFILE) or DEFAULT_PROFILE
    ).strip().upper()


def aug13_early_hybrid_enabled():
    return current_profile() in {
        AUG13_EARLY_HYBRID_V1,
        AUG13_ADAPTIVE_OVERFIT_V1,
    }


def adaptive_overfit_enabled():
    """Enable the bounded value-migration lane without weakening safety."""
    return current_profile() == AUG13_ADAPTIVE_OVERFIT_V1


def passive_reason_is_retryable(reason):
    """Temporary no-fill conditions must not tombstone a valid opportunity."""
    return str(reason or '').upper() in {
        'EXPIRED_UNFILLED',
        'RETENTION_POWER_EXPIRED',
        'PASSIVE_TOXICITY_EXPIRED',
        'TRADE_POWER_BELOW_FLOOR_2S',
        'REALIZABLE_EDGE_NEGATIVE',
        'MAINNET_MAKER_EDGE_BELOW_BUFFER',
        'MAINNET_RISK_BUDGET_INVALID',
        'SYSTEM_NOT_READY',
        'EXECUTION_FEED_STALE',
    }


def passive_reason_is_structural_terminal(reason):
    return str(reason or '').upper() in {
        'THESIS_INVALIDATED',
        'OPPOSING_MOMENTUM_CONFLICT',
        'GEOMETRY_INVALIDATED',
    }
