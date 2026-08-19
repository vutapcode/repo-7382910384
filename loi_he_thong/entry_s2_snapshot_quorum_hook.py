"""Reuse the Entry Council S2 snapshot instead of rescanning live flow after Edge approval."""

VERSION = "ENTRY_S2_SNAPSHOT_QUORUM_HOOK_V1"


def install(risk_launcher_module):
    if getattr(risk_launcher_module, "_entry_s2_snapshot_quorum_hooked", False):
        return VERSION

    edge = risk_launcher_module.edge
    base = risk_launcher_module.base

    def _entry_quorum_ok(result, state, now):
        allowed, report = edge.authorize(result, state)
        state.entry_edge_tier = report
        state.entry_edge_class = report.get("edge_class")
        state.entry_edge_cost_ok = report.get("cost_ok")
        state.entry_edge_updated_at = now
        if not allowed:
            return False

        # Entry Council already made this decision from a causal multi-venue S2 snapshot.
        # Reuse that exact evidence so a rolling 3s boundary cannot reject it milliseconds later.
        s_votes = (result or {}).get("s_votes") or {}
        s2 = s_votes.get("S2_multi_venue_executed_flow") or {}
        flow = s2.get("metrics") or {}
        venues = flow.get("venues") or {}
        required = 1 if report.get("entry_mode") == "FAST" else 2

        state.entry_tier_s_volume_quality = {
            "source": "ENTRY_S2_SNAPSHOT",
            "floor_btc": flow.get("volume_floor_btc"),
            "venues": {
                name: float((payload or {}).get("volume_btc", 0.0) or 0.0)
                for name, payload in venues.items()
                if isinstance(payload, dict)
            },
            "supporters": list(flow.get("supporters") or ()),
            "strong_supporters": list(flow.get("strong_supporters") or ()),
            "required": required,
            "snapshot_ts": float(flow.get("ts", (result or {}).get("ts", now)) or now),
        }
        return True

    risk_launcher_module._entry_quorum_ok = _entry_quorum_ok
    base._entry_quorum_ok = _entry_quorum_ok
    risk_launcher_module._entry_s2_snapshot_quorum_hooked = True
    return VERSION
