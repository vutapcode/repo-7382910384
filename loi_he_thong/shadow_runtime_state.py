"""Atomic persistence and conservative cost accounting for Mainnet shadow runtime."""
import json
import os
from pathlib import Path
import tempfile
import time

from loi_he_thong import execution_transaction

VERSION = "SHADOW_RUNTIME_STATE_V14_AUTHORITY_CONTRACTS"
SUPPORTED_VERSIONS = {
    "SHADOW_RUNTIME_STATE_V1",
    "SHADOW_RUNTIME_STATE_V2",
    "SHADOW_RUNTIME_STATE_V3_PROMOTION_EVIDENCE",
    "SHADOW_RUNTIME_STATE_V4_VERSION_BOUND_CALIBRATION",
    "SHADOW_RUNTIME_STATE_V5_VERIFIED_COST_PLAN",
    "SHADOW_RUNTIME_STATE_V6_ENTRY_ECONOMICS",
    "SHADOW_RUNTIME_STATE_V7_ENTRY_ECONOMICS_V3",
    "SHADOW_RUNTIME_STATE_V8_ENTRY_ECONOMICS_V4",
    "SHADOW_RUNTIME_STATE_V9_ENTRY_ECONOMICS_V5",
    "SHADOW_RUNTIME_STATE_V10_ENTRY_ECONOMICS_V6_AVAILABILITY_TIME",
    "SHADOW_RUNTIME_STATE_V11_ENTRY_ECONOMICS_V7_CAUSAL_PROOF_SEMANTICS",
    "SHADOW_RUNTIME_STATE_V12_ENTRY_ECONOMICS_V8_TIME_TO_EVENT",
    "SHADOW_RUNTIME_STATE_V13_EXECUTION_PROTECTION_TRANSACTION",
    VERSION,
}

# Core fields match the active Risk/Guardian position contract. The `whale_*`,
# `risk_px_samples`, and `exhaustion_meta` names below are read/write migration
# baggage for old checkpoints only; active Risk never reads them or grants them
# exit authority. Keep them until the persisted-state migration window closes.
PERSIST_FIELDS = (
    "active", "side", "qty", "initial_qty",
    "entry_price", "execution_entry_price", "opened_at", "position_cycle_id",
    "r", "hard_sl", "best", "best_r", "floor_r", "floor", "stage",
    "tier_mode", "fee_r", "whale_seen",
    "whale_exhaustion_since", "whale_exhaustion_pressure",
    "risk_px_samples", "exhaustion_meta",
    "guardian_s_signature", "guardian_s_candidate_since",
    "guardian_s_phase", "guardian_s_pullback_started_at",
    "guardian_s_pullback_start_price", "guardian_s_worst_adverse_price",
    "guardian_s_worst_adverse_bps", "guardian_s_reclaim_peak_fraction",
    "guardian_s_reclaim_hold_since", "guardian_s_recovery_result",
    "guardian_s_failed_recovery_reason", "guardian_s_pullback_flow_state",
    "guardian_s_pullback_opposing_flow_state",
    "live", "entry_client_order_id", "hard_sl_algo_id",
    "hard_sl_client_algo_id", "mainnet_risk_plan", "entry_lane",
    "canonical_opportunity_id",
    "causal_episode_id",
    "decision_cycle_id", "entry_regime", "entry_edge_class",
    "entry_causal_thesis", "authority_contracts",
    "shadow_cost_plan", "execution_cost_plan",
    "shadow_ledger_type", "would_live_authorize",
    "edge_first_positive_net_at", "edge_time_to_positive_net_seconds",
)

# Short-lived evidence must never bridge a process/network outage.
RESET_ON_RESTORE = {
    "whale_exhaustion_since": 0.0,
    "whale_exhaustion_pressure": 0.0,
    "risk_px_samples": [],
    "exhaustion_meta": {"reason": "RESTART_REQUIRES_FRESH_EVIDENCE"},
    "guardian_s_signature": (),
    "guardian_s_candidate_since": 0.0,
    "guardian_s_phase": "HEALTHY",
    "guardian_s_pullback_started_at": 0.0,
    "guardian_s_pullback_start_price": 0.0,
    "guardian_s_worst_adverse_price": 0.0,
    "guardian_s_worst_adverse_bps": 0.0,
    "guardian_s_reclaim_peak_fraction": 0.0,
    "guardian_s_reclaim_hold_since": 0.0,
    "guardian_s_recovery_result": "NONE",
    "guardian_s_failed_recovery_reason": None,
    "guardian_s_pullback_flow_state": "UNKNOWN",
    "guardian_s_pullback_opposing_flow_state": "UNKNOWN",
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
        "event_seq": int(getattr(state, "mainnet_shadow_event_seq", 0) or 0),
        "balance": float(getattr(state, "mainnet_shadow_balance_usdt", 0.0) or 0.0),
        "realized_pnl": float(getattr(state, "mainnet_shadow_realized_pnl", 0.0) or 0.0),
        "trades": int(getattr(state, "mainnet_shadow_trades", 0) or 0),
        "wins": int(getattr(state, "mainnet_shadow_wins", 0) or 0),
        "losses": int(getattr(state, "mainnet_shadow_losses", 0) or 0),
        "breakevens": int(getattr(state, "mainnet_shadow_breakevens", 0) or 0),
        "gross_profit": float(getattr(state, "mainnet_shadow_gross_profit", 0.0) or 0.0),
        "gross_loss": float(getattr(state, "mainnet_shadow_gross_loss", 0.0) or 0.0),
        "stress_25bps_pnl": float(
            getattr(state, "mainnet_shadow_stress_25bps_pnl", 0.0) or 0.0
        ),
        "shadow_day_start_ms": int(
            getattr(state, "mainnet_shadow_day_start_ms", 0) or 0
        ),
        "shadow_day_realized_pnl": float(
            getattr(state, "mainnet_shadow_day_realized_pnl", 0.0) or 0.0
        ),
        "shadow_daily_locked": bool(
            getattr(state, "mainnet_shadow_daily_locked", False)
        ),
        "canonical_opportunities": int(
            getattr(state, "canonical_opportunity_count", 0) or 0
        ),
        "canonical_qualified": int(
            getattr(state, "canonical_opportunity_qualified", 0) or 0
        ),
        "canonical_captured": int(
            getattr(state, "canonical_opportunity_captured", 0) or 0
        ),
        "canonical_last_consumed_opportunity_id": int(
            getattr(state, "canonical_last_consumed_opportunity_id", 0) or 0
        ),
        "canonical_last_captured_opportunity_id": int(
            getattr(state, "canonical_last_captured_opportunity_id", 0) or 0
        ),
        "canonical_opportunity_active": bool(
            getattr(state, "canonical_opportunity_active", False)
        ),
        "canonical_opportunity_signature": _jsonable(
            getattr(state, "canonical_opportunity_signature", None)
        ),
        "canonical_opportunity_active_qualified": bool(
            getattr(state, "canonical_opportunity_active_qualified", False)
        ),
        "decision_evaluations": int(
            getattr(state, "mainnet_shadow_decision_evaluations", 0) or 0
        ),
        "near_misses": int(getattr(state, "mainnet_shadow_near_misses", 0) or 0),
        "decision_funnel": dict(
            getattr(state, "mainnet_shadow_funnel_counts", {}) or {}
        ),
        "guardian_latency_samples_total": int(
            getattr(state, "guardian_latency_samples_total", 0) or 0
        ),
        "edge_calibration_rows": [
            list(row) for row in list(
                getattr(state, "_edge_cal_v2_rows", ()) or ()
            )[-768:]
        ],
        "edge_calibration_code_version": str(
            getattr(state, "code_version", "") or ""
        ),
        "edge_calibration_config_version": str(
            getattr(state, "strategy_config_version", "") or ""
        ),
        "entry_economics_v2_rows": list(
            getattr(state, "_entry_economics_v2_rows", ()) or ()
        )[-1024:],
        "entry_economics_code_version": str(
            getattr(state, "code_version", "") or ""
        ),
        "entry_economics_config_version": str(
            getattr(state, "strategy_config_version", "") or ""
        ),
        "position": None,
        "execution_transaction": execution_transaction.snapshot(state),
        "execution_control_plane": dict(
            getattr(state, "wstrade_execution_control_plane", {}) or {}
        ),
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
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("SHADOW_RUNTIME_STATE_CORRUPT") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("SHADOW_RUNTIME_STATE_CORRUPT:ROOT_NOT_OBJECT")
    version = raw.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise RuntimeError(f"SHADOW_RUNTIME_STATE_UNSUPPORTED:{version}")

    state = base.app.state
    state.mainnet_shadow_checkpoint_ts = float(raw.get("ts", 0.0) or 0.0)
    for key, attr in (
        ("balance", "mainnet_shadow_balance_usdt"),
        ("realized_pnl", "mainnet_shadow_realized_pnl"),
        ("trades", "mainnet_shadow_trades"),
        ("wins", "mainnet_shadow_wins"),
        ("losses", "mainnet_shadow_losses"),
        ("breakevens", "mainnet_shadow_breakevens"),
        ("gross_profit", "mainnet_shadow_gross_profit"),
        ("gross_loss", "mainnet_shadow_gross_loss"),
        ("stress_25bps_pnl", "mainnet_shadow_stress_25bps_pnl"),
        ("shadow_day_start_ms", "mainnet_shadow_day_start_ms"),
        ("shadow_day_realized_pnl", "mainnet_shadow_day_realized_pnl"),
        ("shadow_daily_locked", "mainnet_shadow_daily_locked"),
        ("canonical_opportunities", "canonical_opportunity_count"),
        ("canonical_qualified", "canonical_opportunity_qualified"),
        ("canonical_captured", "canonical_opportunity_captured"),
        (
            "canonical_last_consumed_opportunity_id",
            "canonical_last_consumed_opportunity_id",
        ),
        (
            "canonical_last_captured_opportunity_id",
            "canonical_last_captured_opportunity_id",
        ),
        ("decision_evaluations", "mainnet_shadow_decision_evaluations"),
        ("near_misses", "mainnet_shadow_near_misses"),
        ("decision_funnel", "mainnet_shadow_funnel_counts"),
        ("guardian_latency_samples_total", "guardian_latency_samples_total"),
        ("event_seq", "mainnet_shadow_event_seq"),
    ):
        if key in raw:
            setattr(state, attr, raw[key])
    calibration_rows = raw.get("edge_calibration_rows", [])
    saved_code = str(raw.get("edge_calibration_code_version", "") or "")
    saved_config = str(raw.get("edge_calibration_config_version", "") or "")
    active_code = str(getattr(state, "code_version", "") or "")
    active_config = str(getattr(state, "strategy_config_version", "") or "")
    calibration_version_match = bool(
        saved_code and saved_config
        and saved_code == active_code
        and saved_config == active_config
    )
    if isinstance(calibration_rows, list) and calibration_version_match:
        state._edge_cal_v2_rows = [
            tuple(row) for row in calibration_rows[-768:]
            if isinstance(row, (list, tuple)) and len(row) in (5, 8, 9)
        ]
    else:
        state._edge_cal_v2_rows = []
        if calibration_rows:
            state.edge_cal_v2_excluded_version_mismatch = len(calibration_rows)
            state.edge_cal_v2_last_exclusion = {
                "reason": "CODE_OR_CONFIG_VERSION_MISMATCH",
                "saved_code_version": saved_code or None,
                "saved_config_version": saved_config or None,
                "active_code_version": active_code or None,
                "active_config_version": active_config or None,
            }
    state.edge_cal_v2_code_version = active_code
    state.edge_cal_v2_config_version = active_config

    economics_rows = raw.get("entry_economics_v2_rows", [])
    economics_version_match = bool(
        str(raw.get("entry_economics_code_version", "") or "") == active_code
        and str(raw.get("entry_economics_config_version", "") or "") == active_config
        and active_code and active_config
    )
    if isinstance(economics_rows, list) and economics_version_match:
        state._entry_economics_v2_rows = [
            dict(row) for row in economics_rows[-1024:]
            if isinstance(row, dict)
            and row.get("economic_contract_version")
            == "ENTRY_ECONOMICS_V8_TIME_TO_EVENT"
        ]
    else:
        state._entry_economics_v2_rows = []
        if economics_rows:
            state.entry_economics_v2_last_exclusion = {
                "reason": "CODE_OR_CONFIG_VERSION_MISMATCH",
                "excluded_rows": len(economics_rows),
            }

    transaction = raw.get("execution_transaction")
    if isinstance(transaction, dict):
        state.wstrade_execution_transaction = dict(transaction)
        if execution_transaction.requires_reconciliation(transaction):
            state.wstrade_execution_recovery_required = True
            state.execution_unknown = True
            state.wstrade_live_entry_allowed = False
            state.execution_unknown_reason = (
                "EXECUTION_TRANSACTION_RESTORED_UNFINISHED"
            )
    control_plane = raw.get("execution_control_plane")
    if (
        isinstance(control_plane, dict)
        and control_plane.get("version") == "EXECUTION_CONTROL_PLANE_V1"
    ):
        # Historical health is audit context only. A new process must collect
        # fresh control-plane samples before this can authorize live entry.
        state.wstrade_execution_control_plane_restored = dict(control_plane)
        state.wstrade_execution_control_plane = {
            "version": control_plane.get("version"),
            "health": "UNKNOWN",
            "reason": "PROCESS_RESTART_REQUIRES_FRESH_MEASUREMENT",
            "entry_allowed": False,
            "sample_count": 0,
        }
    else:
        state.wstrade_execution_control_plane = {}

    # A process restart is a causal data gap. Preserve lifetime counters and
    # consumed IDs, but never bridge a short-lived evidence episode across it.
    state.canonical_opportunity_active = False
    state.canonical_opportunity_signature = None
    state.canonical_opportunity_active_qualified = False
    state.canonical_opportunity_active_episode_id = None
    state.canonical_opportunity_last_evidence_at = 0.0
    state.canonical_opportunity_wait_since = 0.0

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
        if bool(getattr(pos, "live", False)):
            # A live position restored after a process restart is never treated
            # as a shadow sample. Guardian retains exit authority, while
            # reconciliation must verify the exchange position and hard stop
            # before any new entry can be armed.
            state.wstrade_live_position = pos
            state.wstrade_live_entry_allowed = False
            state.wstrade_execution_recovery_required = True
            state.execution_unknown = True
            state.execution_unknown_reason = "LIVE_POSITION_RESTORED"
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
