"""Thin production wrapper that prunes disabled Tier-S tasks before hardened mainnet starts."""
from types import SimpleNamespace

import mainnet_tier_s_shadow_launcher as shadow
from loi_he_thong import tier_s_runtime_prune as prune

_original_apply_runtime = shadow._apply_runtime


def _apply_runtime_with_prune():
    _original_apply_runtime()
    runtime = SimpleNamespace(base=SimpleNamespace(app=shadow.app))
    prune.install(runtime)


shadow._apply_runtime = _apply_runtime_with_prune

import mainnet_tier_s_shadow_hardened_launcher as hardened


if __name__ == "__main__":
    hardened.main()
