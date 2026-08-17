"""Atomic persistence and conservative cost accounting for Mainnet shadow runtime."""
import json
import os
import Path from pathlib
import tempfile
import time

VERSION = "SHADOW_RUNTIME_STATE_V1"

PERSIST_FIELDS = (
    "active","side","qty","entry_price","entry_time","entry_fee","entry_notional","leverage",
    "risk_r","risk_hard_sl","risk_best_r","risk_best_price","risk_profit_floor_r",
    "risk_profit_floor_price","risk_whale_seen","risk_tier_mode","risk_prev_supportive",
    "guardian_s_signature","guardian_s_candidate_since",
)
RESET_ON_RESTORE = (
    "risk_whale_exhaustion_since",
    "risk_whale_exhaustion_pressure",
    "risk_price_samples",
)

def _path():
    root = Path(os.environ.get("SMC_JOURNAL_DIR") or (Path.home()/".local"/"state"/"smc2026"/"mainnet_shadow"))
    root.mkdir(parents=True, exist_ok=True)
    return root/"runtime_state.json"

def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name+".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",",":"))
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp): os.unlink(tmp)
        except OSError:
            pass

def snapshot(base):
    s = base.app.state
    pos = getattr(s, "mainnet_shadow_position", None)
    data = {
        "version": VERSION,
        "ts": time.time(),
        "balance": float(getattr(s, "mainnet_shadow_balance", 0.0) or 0.0),
        "realized_pnl": float(getattr(s, "mainnet_shadow_realized_pnl", 0.0) or 0.0),
        "trades": int(getattr(s, "mainnet_shadow_trades", 0) or 0),
        "wins": int(getattr(s, "mainnet_shadow_wins", 0) or 0),
        "losses": int(getattr(s, "mainnet_shadow_losses", 0) or 0),
        "position": None,
    }
    if pos is not None and bool(getattr(pos,"active",False)):
        p = {}
        for name in PERSIST_FIELDS:
            v = getattr(pos, name, None)
            if isinstance(v, tuple): v = list(v)
            p[name] = v
        data["position"] = p
    return data

def save(base):
    _atomic_json(_path(), snapshot(base))

def restore(base):
    p = _path()
    if not p.exists():
        return False
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if raw.get("version") != VERSION:
        return False
    s = base.app.state
    for k, attr in (
        ("balance","mainnet_shadow_balance"),
        ("realized_pnl","mainnet_shadow_realized_pnl"),
        ("trades","mainnet_shadow_trades"),
        ("wins","mainnet_shadow_wins"),
        ("losses","mainnet_shadow_losses"),
    ):
        if k in raw:
            setattr(s, attr, raw[k])
    pdata = raw.get("position")
    if pdata:
        pos = type("ShadowRecoveredPosition", (), {})()
        for k,v in pdata.items():
            if k == "guardian_s_signature" and isinstance(v, list): v=tuple(v)
            setattr(pos,k,v)
        for k in RESET_ON_RESTORE:
            setattr(pos,k, [] if k=="risk_price_samples" else 0.0)
        pos.active = bool(getattr(pos,"active",True))
        s.mainnet_shadow_position = pos
        s.mainnet_shadow_recovered = True
        s.mainnet_shadow_recovered_at = time.time()
    return True

def install_cost_accounting(base, model_cost_bps=18.0):
    """Wrap shadow close so realized net uses the same conservative budget as entry gating."""
    orig = base._close_shadow
    fee_roundtrip_bps = 2.0 * float(getattr(base, "SHADOW_FEE_BPS_PER_SIDE", 5.0))
    extra_bps = max(0.0, float(model_cost_bps) - fee_roundtrip_bps)
    if extra_bps <= 0:
        return orig
    def wrapped(pos, result, now):
        s = base.app.state
        out = orig(pos, result, now)
        notional = abs(float(getattr(pos,"qty",0.0) or 0.0) * float(getattr(pos,"entry_price",0.0) or 0.0))
        extra = notional * extra_bps / 10000.0
        if extra > 0:
            s.mainnet_shadow_balance = float(getattr(s,"mainnet_shadow_balance",0.0) or 0.0) - extra
            s.mainnet_shadow_realized_pnl = float(getattr(s,"mainnet_shadow_realized_pnl",0.0) or 0.0) - extra
            s.mainnet_shadow_last_extra_cost = extra
            s.mainnet_shadow_last_model_cost_bps = float(model_cost_bps)
        return out
    base._close_shadow = wrapped
    return wrapped
