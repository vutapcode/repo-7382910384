"""Low-CPU, read-only mirror of the live shadow execution journal.

This process has no imports from the strategy runtime and no authority over
market data, decisions, execution, or risk.  It only tails the durable journal
and writes a compact operator-facing copy under /home/ubuntu.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import tempfile
import time


SOURCE = Path(os.getenv(
    "WSTRADE_AUDIT_SOURCE",
    "/home/ubuntu/.local/state/smc2026/mainnet_shadow/events.jsonl",
))
OUTPUT_DIR = Path(os.getenv(
    "WSTRADE_AUDIT_OUTPUT_DIR", "/home/ubuntu/wstrade_trade_log"
))
POLL_SECONDS = max(0.5, float(os.getenv("WSTRADE_AUDIT_POLL_SECONDS", "1.0")))
CHECKPOINT = OUTPUT_DIR / ".mirror_state.json"
ACTIVITY = OUTPUT_DIR / "activity.jsonl"
TRADES = OUTPUT_DIR / "trades.jsonl"
LATEST_JSON = OUTPUT_DIR / "latest_trade.json"
LATEST_TEXT = OUTPUT_DIR / "latest_trade.txt"
README = OUTPUT_DIR / "README.txt"

VN_TZ = timezone(timedelta(hours=7))
STOP = False


def _iso(ts, tz=timezone.utc):
    try:
        return datetime.fromtimestamp(float(ts), tz=tz).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _append_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str
        ) + "\n")
        handle.flush()


def _vote(vote):
    vote = dict(vote or {})
    metrics = dict(vote.get("metrics") or {})
    return {
        "status": vote.get("status"),
        "reason": vote.get("reason"),
        "confidence": vote.get("confidence"),
        "moves": metrics.get("moves"),
        "supporters": metrics.get("supporters"),
        "opponents": metrics.get("opponents"),
        "strong_supporters": metrics.get("strong_supporters"),
        "strong_opponents": metrics.get("strong_opponents"),
        "venues": metrics.get("venues"),
    }


def compact_decision(row):
    record = dict((row or {}).get("decision_record") or {})
    inputs = dict(record.get("inputs") or {})
    output = dict(record.get("output") or {})
    causal = dict((row or {}).get("cash_perp_handoff") or {})
    bias = dict(inputs.get("bias") or {})
    return {
        "decision_cycle_id": (row or {}).get("cycle_id"),
        "decision": (row or {}).get("decision"),
        "reason": (row or {}).get("reason"),
        "side": (row or {}).get("side"),
        "mode": (row or {}).get("entry_mode"),
        "phase": (row or {}).get("phase"),
        "confidence": output.get("confidence"),
        "bias": {
            "direction": bias.get("direction"),
            "confidence": bias.get("confidence"),
            "reason": bias.get("reason"),
            "story": bias.get("story"),
        },
        "price_quorum": _vote(inputs.get("s1_price_quorum")),
        "executed_flow_quorum": _vote(inputs.get("s2_executed_flow_quorum")),
        "oi": inputs.get("open_interest"),
        "oi_intent": inputs.get("oi_intent") or (row or {}).get("oi_intent"),
        "evidence_groups": inputs.get("evidence_groups") or (
            (row or {}).get("evidence_groups")
        ),
        "persistence": inputs.get("flow_persistence") or (
            (row or {}).get("flow_persistence")
        ),
        "handoff": inputs.get("cash_perp_handoff") or causal,
        "exchange_independence": inputs.get("exchange_independence") or (
            (row or {}).get("exchange_independence")
        ),
        "edge_class": output.get("edge_class") or (row or {}).get("edge_class"),
        "cost": output.get("cost"),
        "miss_taxonomy": output.get("miss_taxonomy") or (
            (row or {}).get("miss_taxonomy")
        ),
        "strategy_code_version": record.get("strategy_code_version"),
        "strategy_config_version": record.get("strategy_config_version"),
    }


def compact_position(row):
    guardian = dict((row or {}).get("guardian_state") or {})
    risk = dict((row or {}).get("risk_state") or {})
    regime = dict((row or {}).get("regime") or {})
    return {
        "ts": (row or {}).get("ts"),
        "utc": _iso((row or {}).get("ts")),
        "utc_plus_7": _iso((row or {}).get("ts"), VN_TZ),
        "price": (row or {}).get("price"),
        "holding_seconds": (row or {}).get("holding_time_seconds"),
        "best_r": (row or {}).get("best_r"),
        "floor_r": (row or {}).get("floor_r"),
        "hard_sl": (row or {}).get("hard_sl"),
        "guardian": {
            "decision": guardian.get("decision"),
            "reason": guardian.get("reason"),
            "confidence": guardian.get("confidence"),
            "supportive_count": guardian.get("supportive_count"),
            "adverse_count": guardian.get("adverse_count"),
            "entry_thesis": guardian.get("entry_thesis"),
            "runner_shield_active": guardian.get("runner_shield_active"),
            "deterioration_elapsed_seconds": guardian.get(
                "deterioration_elapsed_seconds"
            ),
        },
        "risk": {
            "decision": risk.get("decision"),
            "reason": risk.get("reason"),
            "stage": risk.get("stage"),
            "tier_mode": risk.get("tier_mode"),
        },
        "regime": regime.get("regime"),
        "oi_signature": regime.get("oi_signature"),
    }


def _entry_record(row, basis):
    return {
        "ts": row.get("ts"),
        "utc": _iso(row.get("ts")),
        "utc_plus_7": _iso(row.get("ts"), VN_TZ),
        "trade_id": row.get("cycle_id"),
        "decision_cycle_id": row.get("decision_cycle_id"),
        "causal_episode_id": row.get("causal_episode_id"),
        "side": row.get("side"),
        "entry_price": row.get("price"),
        "target_qty_btc": row.get("target_qty_btc"),
        "actual_qty_btc": row.get("actual_qty_btc") or row.get("qty_btc"),
        "mode": row.get("entry_mode"),
        "phase": row.get("phase"),
        "confidence": row.get("confidence"),
        "edge_class": row.get("edge_class"),
        "fee_model": row.get("fee_model"),
        "slippage_model": row.get("slippage_model"),
        "execution": row.get("shadow_execution"),
        "hard_sl": row.get("hard_sl"),
        "risk_plan": row.get("risk_plan"),
        "feasibility": row.get("feasibility"),
        "causal_thesis": row.get("entry_causal_thesis"),
        "regime": row.get("regime_at_entry"),
        "basis": basis,
    }


def _exit_record(row):
    guardian = dict(row.get("guardian_state") or row.get("guardian") or {})
    return {
        "ts": row.get("ts"),
        "utc": _iso(row.get("ts")),
        "utc_plus_7": _iso(row.get("ts"), VN_TZ),
        "exit_price": row.get("exit_price"),
        "gross_pnl_usdt": row.get("gross_pnl_usdt"),
        "fees_usdt": row.get("fees_usdt"),
        "net_pnl_usdt": row.get("net_pnl_usdt"),
        "gross_pnl_bps": row.get("gross_pnl_bps"),
        "net_pnl_bps": row.get("net_pnl_bps"),
        "net_pnl_r": row.get("net_pnl_r"),
        "holding_seconds": row.get("holding_time_seconds"),
        "best_r": row.get("best_r"),
        "floor_r": row.get("floor_r"),
        "balance_usdt": row.get("balance_usdt"),
        "reason": row.get("risk_reason") or guardian.get("reason"),
        "guardian": {
            "decision": guardian.get("decision"),
            "reason": guardian.get("reason"),
            "confidence": guardian.get("confidence"),
            "votes": guardian.get("votes"),
            "entry_thesis": guardian.get("entry_thesis"),
            "adverse_profile": guardian.get("adverse_profile"),
            "runner_shield_active": guardian.get("runner_shield_active"),
            "deterioration_elapsed_seconds": guardian.get(
                "deterioration_elapsed_seconds"
            ),
        },
        "regime_at_exit": row.get("regime_at_exit"),
    }


def _fmt(value, digits=6):
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_latest(trade):
    entry = dict(trade.get("entry") or {})
    basis = dict(entry.get("basis") or {})
    bias = dict(basis.get("bias") or {})
    price = dict(basis.get("price_quorum") or {})
    flow = dict(basis.get("executed_flow_quorum") or {})
    oi = dict(basis.get("oi_intent") or {})
    handoff = dict(basis.get("handoff") or {})
    exit_row = dict(trade.get("exit") or {})
    timeline = list(trade.get("timeline") or [])
    lines = [
        "WSTRADE SHADOW TRADE AUDIT (VIRTUAL ONLY)",
        f"Status: {trade.get('status', 'UNKNOWN')}",
        f"Trade ID: {trade.get('trade_id')}",
        f"Side: {entry.get('side')} | Qty BTC: {_fmt(entry.get('actual_qty_btc'))}",
        f"Entry UTC+7: {entry.get('utc_plus_7')} | Price: {_fmt(entry.get('entry_price'), 2)}",
        f"Mode/phase/edge: {entry.get('mode')} / {entry.get('phase')} / {entry.get('edge_class')}",
        f"Hard SL: {_fmt(entry.get('hard_sl'), 2)}",
        "",
        "CAN CU VAO LENH",
        f"Bias: {bias.get('direction')} conf={_fmt(bias.get('confidence'), 4)} reason={bias.get('reason')}",
        f"Price quorum: {price.get('status')} supporters={price.get('supporters')} opponents={price.get('opponents')}",
        f"Flow quorum: {flow.get('status')} supporters={flow.get('supporters')} opponents={flow.get('opponents')}",
        f"OI intent: {oi.get('regime')} fresh={oi.get('edge_fresh')} closing={oi.get('closing')}",
        f"Cash/perp handoff: {handoff.get('status')} reason={handoff.get('reason')}",
        f"Decision: {basis.get('decision')} reason={basis.get('reason')}",
        "",
        f"DIEN BIEN: {len(timeline)} state changes recorded",
    ]
    for point in timeline[-8:]:
        guard = dict(point.get("guardian") or {})
        lines.append(
            f"- {point.get('utc_plus_7')} px={_fmt(point.get('price'), 2)} "
            f"bestR={_fmt(point.get('best_r'), 3)} guard={guard.get('decision')}:"
            f"{guard.get('reason')}"
        )
    if exit_row:
        lines.extend([
            "",
            "KET QUA",
            f"Exit UTC+7: {exit_row.get('utc_plus_7')} | Price: {_fmt(exit_row.get('exit_price'), 2)}",
            f"Reason: {exit_row.get('reason')}",
            f"Gross/fee/net USDT: {_fmt(exit_row.get('gross_pnl_usdt'))} / "
            f"{_fmt(exit_row.get('fees_usdt'))} / {_fmt(exit_row.get('net_pnl_usdt'))}",
            f"Net bps/R: {_fmt(exit_row.get('net_pnl_bps'), 3)} / {_fmt(exit_row.get('net_pnl_r'), 3)}",
            f"Balance ao sau lenh: {_fmt(exit_row.get('balance_usdt'), 4)} USDT",
        ])
    return "\n".join(lines) + "\n"


class AuditMirror:
    def __init__(self, source=SOURCE, output_dir=OUTPUT_DIR):
        self.source = Path(source)
        self.output_dir = Path(output_dir)
        self.checkpoint = self.output_dir / CHECKPOINT.name
        self.activity = self.output_dir / ACTIVITY.name
        self.trades = self.output_dir / TRADES.name
        self.latest_json = self.output_dir / LATEST_JSON.name
        self.latest_text = self.output_dir / LATEST_TEXT.name
        self.offset = 0
        self.active = {}
        self.decisions = OrderedDict()
        self.completed = set()
        self._load()

    def _load(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.trades.exists():
            with self.trades.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        trade_id = json.loads(line).get("trade_id")
                    except (json.JSONDecodeError, OSError):
                        continue
                    if trade_id:
                        self.completed.add(str(trade_id))
        if not self.checkpoint.exists():
            return
        try:
            state = json.loads(self.checkpoint.read_text(encoding="utf-8"))
            self.offset = max(0, int(state.get("source_offset", 0) or 0))
            self.active = dict(state.get("active_trades") or {})
        except (OSError, ValueError, json.JSONDecodeError):
            self.offset = 0
            self.active = {}

    def _save(self):
        _atomic_json(self.checkpoint, {
            "schema_version": 1,
            "source": str(self.source),
            "source_offset": self.offset,
            "updated_at": time.time(),
            "active_trades": self.active,
        })

    def _latest(self, trade):
        _atomic_json(self.latest_json, trade)
        _atomic_text(self.latest_text, render_latest(trade))

    def _remember_decision(self, row):
        cycle = row.get("cycle_id")
        if not cycle:
            return
        self.decisions[str(cycle)] = compact_decision(row)
        while len(self.decisions) > 256:
            self.decisions.popitem(last=False)

    def process(self, row, source_offset=None):
        event = str(row.get("event") or "")
        if event == "DECISION_EVALUATED":
            self._remember_decision(row)
            return
        if event == "ENTRY":
            trade_id = str(row.get("cycle_id") or "")
            basis = self.decisions.get(str(row.get("decision_cycle_id") or ""))
            trade = {
                "schema_version": "WSTRADE_OPERATOR_TRADE_AUDIT_V1",
                "virtual_only": True,
                "status": "OPEN",
                "trade_id": trade_id,
                "entry": _entry_record(row, basis),
                "timeline": [],
                "exit": None,
            }
            self.active[trade_id] = trade
            self._latest(trade)
            _append_json(self.activity, {
                "event": "ENTRY", "source_offset": source_offset,
                "trade": trade,
            })
            return
        if event == "POSITION_STATE":
            trade_id = str(row.get("cycle_id") or "")
            trade = self.active.get(trade_id)
            if trade is None:
                trade = {
                    "schema_version": "WSTRADE_OPERATOR_TRADE_AUDIT_V1",
                    "virtual_only": True, "status": "OPEN",
                    "trade_id": trade_id, "entry": None,
                    "timeline": [], "exit": None,
                }
                self.active[trade_id] = trade
            trade["timeline"].append(compact_position(row))
            trade["timeline"] = trade["timeline"][-600:]
            self._latest(trade)
            return
        if event == "EXIT":
            trade_id = str(row.get("cycle_id") or "")
            trade = self.active.pop(trade_id, None) or {
                "schema_version": "WSTRADE_OPERATOR_TRADE_AUDIT_V1",
                "virtual_only": True, "trade_id": trade_id,
                "entry": {
                    "trade_id": trade_id,
                    "decision_cycle_id": row.get("decision_cycle_id"),
                    "causal_episode_id": row.get("causal_episode_id"),
                    "side": row.get("side"),
                    "entry_price": row.get("entry_price"),
                    "basis": self.decisions.get(str(
                        row.get("decision_cycle_id") or ""
                    )),
                },
                "timeline": [],
            }
            trade["status"] = "CLOSED"
            trade["exit"] = _exit_record(row)
            if trade_id not in self.completed:
                _append_json(self.trades, trade)
                self.completed.add(trade_id)
            self._latest(trade)
            _append_json(self.activity, {
                "event": "EXIT", "source_offset": source_offset,
                "trade_id": trade_id, "exit": trade["exit"],
            })
            return
        if event in {
            "SHADOW_MAKER_PLACED", "SHADOW_MAKER_CANCELED",
            "ENTRY_SKIPPED", "SHADOW_CAPITAL_ADJUSTMENT",
        }:
            compact = {
                "event": event,
                "source_offset": source_offset,
                "ts": row.get("ts"),
                "utc": _iso(row.get("ts")),
                "utc_plus_7": _iso(row.get("ts"), VN_TZ),
                "cycle_id": row.get("cycle_id"),
                "causal_episode_id": row.get("causal_episode_id"),
                "side": row.get("side"),
                "reason": row.get("reason"),
                "miss_taxonomy": row.get("miss_taxonomy"),
                "limit_price": row.get("limit_price"),
                "old_balance_usdt": row.get("old_balance_usdt"),
                "new_balance_usdt": row.get("new_balance_usdt"),
                "deposit_usdt": row.get("deposit_usdt"),
            }
            if event == "ENTRY_SKIPPED":
                compact["basis"] = compact_decision({
                    "cycle_id": row.get("cycle_id"),
                    "decision": "GO",
                    "reason": (row.get("entry") or {}).get("reason"),
                    "side": row.get("side"),
                    "entry_mode": (row.get("entry") or {}).get("entry_mode"),
                    "phase": (row.get("entry") or {}).get("phase"),
                    "decision_record": {},
                })
                compact["counterfactual"] = row.get("counterfactual")
            _append_json(self.activity, compact)

    def run_once(self):
        if not self.source.exists():
            return 0
        size = self.source.stat().st_size
        if size < self.offset:
            self.offset = 0
        count = 0
        with self.source.open("r", encoding="utf-8") as handle:
            handle.seek(self.offset)
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    handle.seek(start)
                    break
                self.offset = handle.tell()
                self.process(row, source_offset=start)
                count += 1
        if count:
            self._save()
        return count


def _readme():
    return """WStrade operator trade log - COPY ONLY, VIRTUAL/SHADOW

Files:
  latest_trade.txt   Human-readable latest trade and rationale
  latest_trade.json  Same latest trade in structured JSON
  trades.jsonl       One complete record per closed virtual trade
  activity.jsonl     Maker placement/cancel, skipped entry, capital changes

Quick commands:
  less /home/ubuntu/wstrade_trade_log/latest_trade.txt
  tail -n 5 /home/ubuntu/wstrade_trade_log/trades.jsonl
  tail -f /home/ubuntu/wstrade_trade_log/activity.jsonl

This service only reads the durable bot journal. It cannot alter feeds,
strategy decisions, positions, orders, Guardian, recorder, or Mainnet state.
"""


def _stop(_signum, _frame):
    global STOP
    STOP = True


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_text(README, _readme())
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    mirror = AuditMirror()
    while not STOP:
        mirror.run_once()
        time.sleep(POLL_SECONDS)
    mirror.run_once()


if __name__ == "__main__":
    main()
