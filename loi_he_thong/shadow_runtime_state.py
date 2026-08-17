"""Atomic persistence and conservative cost accounting for Mainnet shadow runtime."""
import json
import os
from pathlib import Path
import tempfile
import time

VERSION = "SHADOW_RUNTIME_STATE_V2"
SUPPORTED_VERSIONS = {"SHADOW_RUNTIME_STATE_V1", VERSION}

# These names intentionally match the attributes created by shadow_risk_guard.arm/assess.
PERSIST_FIELDS = (
    "active", "side", "qty", "initial_qty",
    "entry_price", "execution_entry_price", "opened_at", "position_cycle_id",
    "r", "hard_sl", "best", "best_r", "floor_r", "floor", "stage",
    "tier_mode", "fee_r", "whale_seen",
    "whale_exhaustion_since", "whale_exhaustion_pressure",
    "risk_px_samples", "exhaustion_meta",
    "guardian_s_signature", "guardian_s_candidate_since",
)

# Short-lived evidence must never bridge a process/network outage.
RESET_ON_RESTORE = {
    "whale_exhaustion_since": 0.0,
    "whale_exhaustion_pressure": 0.0,
    "risk_px_samples": [],
    "exhaustion_meta": {"reason": "RESTART_REQUIRES_FRESH_EVIDENCE"},
    "guardian_s_signature": (),
    "guardian_s_candidate_since": 0.0,
}

# Best-effort migration from the first persistence draft, whose field names did not
# match shadow_risk_guard. Most old snapshots will contain None for these fields;
# only non-None values are migrated.
V1_ALIASES = {
    "risk_r": "r",
    "risk_hard_sl": "hard_sl",
    "risk_best_r": "best_r",
    "risk_best_price": "best",
    "risk_profit_floor_r": "floor_r",
    "risk_profit_floor_price": "floor",
    "risk_whale_seen": "whale_seen",
    "risk_tier_mode": "tier_mode",
    "risk_whale_exhaustion_since": "whale_exhaustion_since",
    "risk_whale_exhaustion_pressure": "whale_exhaustion_pressure",
    "risk_price_samples": "risk_px_samples",
}


def _path():
    root = Path(
        os.environ.get("SMC_JOURNAL_DIR")
        or (Path.home() / ".local" / "state" / "smc2026" / "mainnet_shadow")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root / "runtime_state.json"


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # Persist the directory entry too, so a power loss cannot resurrect the
        # previous inode after os.replace().
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _jsonable(value):
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, tuple):
                out.append(list(item))
            else:
                out.append(item)
        return out
    return value


def snapshot(base):
    state = base.app.state
    pos = getattr(state, "mainnet_shadow_position", None)
    data = {
        "version": VERSION,
        "ts": time.time(),
        "balance": float(getattr(state, "mainnet_shadow_balance_usdt", 0.0) or 0.0),
        "realized_pnl": float(getattr(state, "mainnet_shadow_realized_pnl", 0.0) or 0.0),
        "trades": int(getattr(state, "mainnet_shadow_trades", 0) or 0),
        "wins": int(getattr(state, "mainnet_shadow_wins", 0) or 0),
        "losses": int(getattr(state, "mainnet_shadow_losses", 0) or 0),
        "breakevens": int(getatttr(state, "mainnet_shadow_breakevens", 0) or 0),
        "position": None,
    }
    if pos is not None and bool(getattr(pos, "active", False)):
        data["position"] = {
            name: _jsonable(getattr(pos, name, None))
            for name in PERSIST_FIELDS
        }
    return data


def save(base):
    _atomic_json(_path(), snapshot(base))


def _migrate_position(raw_version, pdata):
    pdata = dict(pdata or {})
    degraded = False
    if raw_version == "SHADOW_RUNTIME_STATE_V1":
        for old_name, new_name in V1_ALIASES.items():
            if new_name not in pdata or pdata.get(new_name) is None:
                value = pdata.get(old_name)
                if value is not None:
                    pdata[new_name] = value
        # V1 often could not preserve the ratchet because it used wrong names.
        if not pdata.get("r") or pdata.get("best_r") is None:
            degraded = True
    return pdata, degraded


def restore(base):
    path = _path()
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    version = raw.get("version")
    if version not in SUPPORTED_VERSIONS:
        return False

    state = base.app.state
    for key, attr in (
        ("balance", "mainnet_shadow_balance_usdt"),
        ("realized_pnl", "mainnet_shadow_realized_pnl"),
        ("trades", "mainnet_shadow_trades"),
        ("wins", "mainnet_shadow_wins"),
        ("losses", "mainnet_shadow_losses"),
        ("breakevens", "mainnet_shadow_breakevens"),
    ):
        if key in raw:
            setattr(state, attr, raw[key])

    pdata = raw.get("position")
    if pdata:
        pdata, degraded = _migrate_position(version, pdata)
        pos = type("ShadowRecoveredPosition", (), {})()
        for name in PERSIST_FIELDS:
            if name in pdata:
                value = pdata[name]
                if name == "guardian_s_signature" and isinstance(value, list):
                    value = tuple(value)
                if name == "risk_px_samples" and isinstance(value, list):
                    value = [
                        tuple(row) if isinstance(row, list) else row
                        for row in value
                    ]
                setattr(pos, name, value)

        # Minimal invariants required by the risk engine. If a V1 snapshot cannot
        # provide them, preserve the open position but force a conservative re-arm.
        pos.active = bool(getattr(pos, "active", True))
        if not getattr(pos, "qty", None):
            pos.qty = float(getattr(base, "QTY_BTC", 0.001))
        if not getattr(pos, "initial_qty", None):
            pos.initial_qty = pos.qty
        if not getattr(pos, "execution_entry_price", None):
            pos.execution_entry_price = float(getattr(pos, "entry_price", 0.0) or 0.0)
        if not getattr(pos, "opened_at", None):
            pos.opened_at = float(raw.get("ts", time.time()) or time.time())
        if not getattr(pos, "position_cycle_id", None):
            pos.position_cycle_id = "shadow:recovered:%d" % int(time.time() * 1000)

        for name, value in RESET_ON_RESTORE.items():
            setattr(pos, name, list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value)

        state.mainnet_shadow_position = pos
        state.mainnet_shadow_recovered = True
        state.mainnet_shadow_recovered_at = time.time()
        state.mainnet_shadow_recovery_degraded = bool(degraded)
        state.mainnet_shadow_recovery_source_version = version
    return True


def install_cost_accounting(base, model_cost_bps=18.0):
    """Top up realized shadow cost only if native per-side accounting is below the model."""
    orig = base._close_shadow
    fee_roundtrip_bps = 2.0 * float(getattr(base, "SHADOW_FEE_BPS_PER_SIDE", 5.0))
    extra_bps = max(0.0, float(model_cost_bps) - fee_roundtrip_bps)
    if extra_bps <= 0:
        return orig

    def wrapped(pos, result, now):
        state = base.app.state
        balance_before = float(getattr(state, "mainnet_shadow_balance_usdt", 0.0) or 0.0)
        out = orig(pos, result, now)
        # If orig did not close (e.g. no fresh execution price), do not charge.
        if bool(getattr(pos, "active", False)):
            return out
        notional = abs(
            float(getattr(pos, "qty", 0.0) or 0.0)
            * float(getattr(pos, "entry_price", 0.0) or 0.0)
        )
        extra = notional * extra_bps / 10000.0
        if extra > 0:
            state.mainnet_shadow_balance_usdt = (
                float(getattr(state, "mainnet_shadow_balance_usdt", balance_before) or balance_before)
                - extra
            )
            state.mainnet_shadow_realized_pnl = (
                float(getattr(state, "mainnet_shadow_realized_pnl", 0.0) or 0.0)
                - extra
            )
            state.mainnet_shadow_last_extra_cost = extra
            state.mainnet_shadow_last_model_cost_bps = float(model_cost_bps)
        return out

    base._close_shadow = wrapped
    return wrapped
