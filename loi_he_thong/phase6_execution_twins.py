"""Offline Phase-6 execution twins.

Replay/recorder only. No hot-path imports, no authority, no forecast selection.
All branches consume only events whose `available_time` is <= branch time.
"""
from __future__ import annotations

import hashlib
import json

VERSION = "PHASE6_EXECUTION_TWINS_V2_EXECUTABLE_PATH"
AUTHORITY = False
BRANCHES = (
    "TAKER_NOW", "WAIT100", "WAIT300", "WAIT500", "WAIT600",
    "MAKER_IF_EXECUTABLE",
)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _eligible(events, at):
    return [e for e in events if float(e.get("available_time", -1)) <= float(at)]


def _latest_bbo(events, at):
    rows = [e for e in _eligible(events, at) if e.get("type") == "BBO"]
    if not rows:
        return None
    row = rows[-1]
    if row.get("valid_until") is not None and float(row["valid_until"]) < float(at):
        return None
    bid, ask = float(row.get("bid", 0)), float(row.get("ask", 0))
    if bid <= 0 or ask <= bid:
        return None
    return row


def _state_at(events, at):
    rows = [e for e in _eligible(events, at) if e.get("type") == "STATE"]
    return rows[-1] if rows else {}


def _entry_price(side, bbo):
    return float(bbo["ask"] if side == "LONG" else bbo["bid"])


def _exit_price(side, bbo):
    return float(bbo["bid"] if side == "LONG" else bbo["ask"])


def _maker_fill(side, limit_price, qty, queue_ahead, placed_at, expires_at, events):
    remaining_queue = max(0.0, float(queue_ahead))
    filled = 0.0
    depletion = []
    for e in events:
        t = float(e.get("available_time", -1))
        if t <= placed_at or t > expires_at or e.get("type") != "TRADE":
            continue
        px, q = float(e.get("price", 0)), max(0.0, float(e.get("qty", 0)))
        aggressor = str(e.get("aggressor") or "").upper()
        eligible = (
            side == "LONG" and aggressor == "SELL" and px <= limit_price
        ) or (
            side == "SHORT" and aggressor == "BUY" and px >= limit_price
        )
        if not eligible:
            continue
        before = remaining_queue
        consumed_queue = min(remaining_queue, q)
        remaining_queue -= consumed_queue
        available_for_us = max(0.0, q - consumed_queue)
        take = min(qty - filled, available_for_us)
        filled += take
        depletion.append({
            "available_time": t,
            "trade_qty": q,
            "queue_before": before,
            "queue_after": remaining_queue,
            "our_fill_qty": take,
            "filled_qty": filled,
        })
        if filled >= qty:
            return {
                "status": "FILLED",
                "filled_qty": qty,
                "fill_time": t,
                "depletion": depletion,
            }
    return {
        "status": "PARTIAL_OR_UNFILLED",
        "filled_qty": filled,
        "fill_time": None,
        "depletion": depletion,
    }


def _post_fill_outcome(branch, side, entry_price, fill_time, events, frozen_cost, guardian_step, hard_risk_step):
    exit_reason = None
    exit_price = None
    hard_stop = False
    support_time = None
    failure_time = None
    direction = 1.0 if side == "LONG" else -1.0
    cost = float(frozen_cost)
    mfe = 0.0
    mae = 0.0
    time_to_positive_net = None
    for e in events:
        event_time = float(e.get("available_time", -1))
        if event_time <= float(fill_time):
            continue
        # Mark the branch only against an executable BBO already available at
        # this observation. Future candle extrema are never used.
        bbo = _latest_bbo(events, event_time)
        if bbo is not None:
            executable_exit = _exit_price(side, bbo)
            excursion = (
                (executable_exit - entry_price) / entry_price * 10000.0
                * direction
            )
            mfe = max(mfe, excursion)
            mae = min(mae, excursion)
            if time_to_positive_net is None and excursion - cost > 0.0:
                time_to_positive_net = event_time - float(fill_time)
        if support_time is None and e.get("support") is True:
            support_time = event_time - float(fill_time)
        if failure_time is None and e.get("failure") is True:
            failure_time = event_time - float(fill_time)
        risk = hard_risk_step(e) if hard_risk_step else None
        if risk and risk.get("exit"):
            hard_stop = True
            exit_reason = risk.get("reason") or "HARD_RISK"
            exit_price = float(risk["exit_price"])
            break
        decision = guardian_step(e) if guardian_step else None
        if decision and decision.get("exit"):
            exit_reason = decision.get("reason") or "GUARDIAN"
            exit_price = float(decision["exit_price"])
            break
    if exit_price is None:
        return {
            "status": "CENSORED_NO_EXIT",
            "branch": branch,
            "hard_stop": hard_stop,
            "capture_ratio": None,
            "gross_pnl_bps": None,
            "net_bps": None,
            "mfe_bps": mfe,
            "mae_bps": mae,
            "time_to_positive_net": time_to_positive_net,
            "time_to_support": support_time,
            "time_to_failure": failure_time,
            "exit_reason": exit_reason,
        }
    gross = (exit_price - entry_price) / entry_price * 10000.0 * direction
    net = gross - cost
    executable_mfe_after_cost = max(0.0, mfe - cost)
    capture_ratio = (
        max(0.0, min(1.0, net / executable_mfe_after_cost))
        if executable_mfe_after_cost > 0.0 else 0.0
    )
    return {
        "status": "CLOSED",
        "branch": branch,
        "hard_stop": hard_stop,
        "capture_ratio": capture_ratio,
        "mfe_bps": mfe,
        "mae_bps": mae,
        "gross_pnl_bps": gross,
        "frozen_cost_bps": cost,
        "cost_applications": 1,
        "net_bps": net,
        "time_to_positive_net": time_to_positive_net,
        "time_to_support": support_time,
        "time_to_failure": failure_time,
        "exit_reason": exit_reason,
    }


def evaluate(opportunity, events, *, guardian_step=None, hard_risk_step=None):
    op = dict(opportunity or {})
    side = str(op.get("side") or "").upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("SIDE_INVALID")
    decision_at = float(op["decision_available_time"])
    events = sorted((dict(e) for e in events), key=lambda e: float(e.get("available_time", -1)))
    identity = {
        k: op.get(k) for k in (
            "market_truth_hash", "causal_episode_id", "wal_identity",
            "candidate_population_hash", "causal_wave_id", "guardian_version",
            "fill_model_version", "frozen_cost_hash",
        )
    }
    out = []
    for branch in BRANCHES:
        delay_ms = 0 if branch == "TAKER_NOW" else int(branch[4:]) if branch.startswith("WAIT") else 0
        at = decision_at + delay_ms / 1000.0
        state = _state_at(events, at)
        if branch.startswith("WAIT") and (
            state.get("wave_alive") is not True
            or state.get("feed_valid") is not True
            or state.get("gap_free") is not True
        ):
            out.append({"branch": branch, "status": "CENSORED_OPPORTUNITY_INVALID", "at": at})
            continue
        bbo = _latest_bbo(events, at)
        if bbo is None:
            out.append({"branch": branch, "status": "CENSORED_NO_EXECUTABLE_BBO", "at": at})
            continue
        if branch == "MAKER_IF_EXECUTABLE":
            ttl_ms = int(op["maker_ttl_ms"])
            qty = float(op["quantity"])
            limit_price = float(bbo["bid"] if side == "LONG" else bbo["ask"])
            fill = _maker_fill(
                side, limit_price, qty, float(op.get("maker_queue_ahead", 0.0)),
                at, at + ttl_ms / 1000.0, events,
            )
            row = {"branch": branch, "at": at, "entry_price": limit_price, **fill}
            if fill["status"] == "FILLED":
                row["outcome"] = _post_fill_outcome(
                    branch, side, limit_price, fill["fill_time"], events,
                    float(op["maker_frozen_cost_bps"]), guardian_step, hard_risk_step,
                )
            out.append(row)
            continue
        entry = _entry_price(side, bbo)
        cost = float(op["taker_frozen_cost_bps"])
        row = {
            "branch": branch, "at": at, "status": "FILLED",
            "entry_price": entry, "fill_time": at,
            "bbo_available_time": float(bbo["available_time"]),
        }
        row["outcome"] = _post_fill_outcome(
            branch, side, entry, at, events, cost, guardian_step, hard_risk_step
        )
        out.append(row)

    body = {
        "version": VERSION,
        "authority": AUTHORITY,
        "identity": identity,
        "branches": out,
        "policy": "OFFLINE_TWINS_NO_BRANCH_SELECTION",
    }
    return {**body, "deterministic_hash": _digest(body)}
