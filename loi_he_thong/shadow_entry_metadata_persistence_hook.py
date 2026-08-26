"""Persist active entry-edge metadata alongside shadow runtime state."""
import json

VERSION = "SHADOW_ENTRY_METADATA_PERSISTENCE_HOOK_V1"
ROOT_KEY = "entry_edge_metadata"


def install(persistence_module):
    if getattr(persistence_module, "_entry_metadata_hooked", False):
        return

    original_snapshot = persistence_module.snapshot
    original_restore = persistence_module.restore

    def snapshot(base):
        data = dict(original_snapshot(base) or {})
        state = base.app.state
        pos = getattr(state, "mainnet_shadow_position", None)
        if pos is not None and bool(getattr(pos, "active", False)):
            edge = getattr(state, "mainnet_shadow_entry_edge", None)
            if isinstance(edge, dict):
                data[ROOT_KEY] = dict(edge)
        return data

    def restore(base):
        restored = bool(original_restore(base))
        if not restored:
            return False

        state = base.app.state
        pos = getattr(state, "mainnet_shadow_position", None)
        if pos is None or not bool(getattr(pos, "active", False)):
            return True

        try:
            raw = json.loads(persistence_module._path().read_text(encoding="utf-8"))
        except Exception:
            return True

        edge = raw.get(ROOT_KEY) if isinstance(raw, dict) else None
        if isinstance(edge, dict):
            state.mainnet_shadow_entry_edge = dict(edge)
            state.mainnet_shadow_entry_edge_restored = True
        return True

    persistence_module.snapshot = snapshot
    persistence_module.restore = restore
    persistence_module._entry_metadata_hooked = True
