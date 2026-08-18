"""Thin production wrapper: install lean app.main before risk/hardening wrap it."""
import mainnet_tier_s_shadow_launcher as shadow
from loi_he_thong import tier_s_runtime_prune as prune

# Install early so risk/hardening wrappers capture the lean orchestrator rather than being overwritten later.
prune.install_app(shadow.app)

import mainnet_tier_s_shadow_hardened_launcher as hardened


if __name__ == "__main__":
    hardened.runtime.base.main()
