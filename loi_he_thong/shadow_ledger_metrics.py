"""Separate exploratory demo outcomes from live-like promotion evidence."""

VERSION = "SHADOW_LEDGER_METRICS_V1"
LEDGERS = {
    "RESEARCH_PROBE": "research_probe",
    "LIVE_LIKE_SHADOW": "live_like",
}
FIELDS = (
    "trades", "wins", "losses", "breakevens",
    "realized_pnl", "gross_profit", "gross_loss", "stress_25bps_pnl",
)


def _prefix(kind):
    return "mainnet_shadow_%s" % LEDGERS.get(
        str(kind or "RESEARCH_PROBE").upper(), "research_probe"
    )


def state_fields():
    return tuple(
        "%s_%s" % (_prefix(kind), field)
        for kind in LEDGERS for field in FIELDS
    ) + ("mainnet_shadow_ledger_metrics_enabled",)


def initialize(state):
    for name in state_fields():
        if name == "mainnet_shadow_ledger_metrics_enabled":
            continue
        if not hasattr(state, name):
            setattr(state, name, 0 if name.endswith(
                ("_trades", "_wins", "_losses", "_breakevens")
            ) else 0.0)
    state.mainnet_shadow_ledger_metrics_enabled = True


def record_close(state, ledger_type, net_pnl, stress_delta=0.0):
    initialize(state)
    prefix = _prefix(ledger_type)
    net = float(net_pnl or 0.0)
    setattr(state, prefix + "_trades", int(
        getattr(state, prefix + "_trades", 0) or 0
    ) + 1)
    setattr(state, prefix + "_realized_pnl", float(
        getattr(state, prefix + "_realized_pnl", 0.0) or 0.0
    ) + net)
    setattr(state, prefix + "_stress_25bps_pnl", float(
        getattr(state, prefix + "_stress_25bps_pnl", 0.0) or 0.0
    ) + float(stress_delta or 0.0))
    if net > 1e-12:
        setattr(state, prefix + "_wins", int(
            getattr(state, prefix + "_wins", 0) or 0
        ) + 1)
        setattr(state, prefix + "_gross_profit", float(
            getattr(state, prefix + "_gross_profit", 0.0) or 0.0
        ) + net)
    elif net < -1e-12:
        setattr(state, prefix + "_losses", int(
            getattr(state, prefix + "_losses", 0) or 0
        ) + 1)
        setattr(state, prefix + "_gross_loss", float(
            getattr(state, prefix + "_gross_loss", 0.0) or 0.0
        ) + abs(net))
    else:
        setattr(state, prefix + "_breakevens", int(
            getattr(state, prefix + "_breakevens", 0) or 0
        ) + 1)


def snapshot(state):
    initialize(state)
    out = {"version": VERSION}
    for kind, slug in LEDGERS.items():
        prefix = _prefix(kind)
        out[slug] = {
            field: getattr(state, prefix + "_" + field, 0)
            for field in FIELDS
        }
    return out


def restore(state, payload):
    initialize(state)
    payload = dict(payload or {})
    for kind, slug in LEDGERS.items():
        row = dict(payload.get(slug) or {})
        prefix = _prefix(kind)
        for field in FIELDS:
            if field in row:
                setattr(state, prefix + "_" + field, row[field])


def promotion_totals(state):
    """Only trades which passed live-equivalent authorization may promote."""
    if not bool(getattr(state, "mainnet_shadow_ledger_metrics_enabled", False)):
        return None
    prefix = _prefix("LIVE_LIKE_SHADOW")
    return {
        "trades": int(getattr(state, prefix + "_trades", 0) or 0),
        "gross_profit": float(
            getattr(state, prefix + "_gross_profit", 0.0) or 0.0
        ),
        "gross_loss": float(
            getattr(state, prefix + "_gross_loss", 0.0) or 0.0
        ),
        "realized": float(
            getattr(state, prefix + "_realized_pnl", 0.0) or 0.0
        ),
        "stress": float(
            getattr(state, prefix + "_stress_25bps_pnl", 0.0) or 0.0
        ),
    }
