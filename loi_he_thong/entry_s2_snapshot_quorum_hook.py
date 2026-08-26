"""Reuse the Entry Council S2 snapshot instead of rescanning live flow after Edge approval."""

VERSION = "ENTRY_S2_SNAPSHOT_QUORUM_HOOK_V2_VENUE_FLOORS"
MAX_SNAPSHOT_AGE_SEC = 0.75


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
        floor = float(flow.get("volume_floor_btc", 0.0) or 0.0)
        floors = flow.get("volume_floor_btc_by_venue") or {}
        snapshot_ts = float(flow.get("ts", (result or {}).get("ts", 0.0)) or 0.0)
        supporters = set(flow.get("supporters") or ())
        valid = {
            name: float((payload or {}).get("volume_btc", 0.0) or 0.0)
            for name, payload in venues.items()
            if (
                isinstance(payload, dict)
                and name in supporters
                and float(floors.get(name, floor) or 0.0) > 0.0
                and float((payload or {}).get("volume_btc", 0.0) or 0.0)
                >= float(floors.get(name, floor) or 0.0)
            )
        }
        snapshot_fresh = bool(
            snapshot_ts > 0.0 and 0.0 <= float(now) - snapshot_ts <= MAX_SNAPSHOT_AGE_SEC
        )

        state.entry_tier_s_volume_quality = {
            "source": "ENTRY_S2_SNAPSHOT",
            "floor_btc": floor,
            "floor_btc_by_venue": dict(floors),
            "venues": valid,
            "supporters": list(flow.get("supporters") or ()),
            "strong_supporters": list(flow.get("strong_supporters") or ()),
            "required": required,
            "snapshot_ts": snapshot_ts,
            "snapshot_fresh": snapshot_fresh,
        }
        return bool(s2.get("status") == "PASS" and snapshot_fresh and len(valid) >= required)

    risk_launcher_module._entry_quorum_ok = _entry_quorum_ok
    base._entry_quorum_ok = _entry_quorum_ok
    risk_launcher_module._entry_s2_snapshot_quorum_hooked = True
    return VERSION
