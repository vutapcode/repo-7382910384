#!/usr/bin/env python3
"""Export a compact, sanitized shadow-trade research dataset.

The source journal stays outside git.  Only explicitly whitelisted decision,
execution, cost and outcome fields are written; balances, account identifiers,
order identifiers and credentials are never copied.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


SCHEMA = "WSTRADE_SANITIZED_SHADOW_TRADE_V3_MARGINAL_FLOW"


def _utc(ts):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _compact(entry, exit_row):
    thesis = entry.get("entry_causal_thesis") or {}
    bias = thesis.get("bias_thesis") or {}
    oi = thesis.get("oi_intent") or {}
    oi_verification = thesis.get("oi_verification_state") or {}
    economic = thesis.get("economic_feature_snapshot") or {}
    flow_efficiency = thesis.get("flow_efficiency") or {}
    execution_urgency = thesis.get("execution_urgency") or {}
    costs = exit_row.get("execution_cost_model") or {}
    guardian = exit_row.get("guardian_state") or exit_row.get("guardian") or {}
    return {
        "schema_version": SCHEMA,
        "virtual_only": True,
        "historical_current_authority": False,
        "cycle_id": entry.get("cycle_id"),
        "decision_cycle_id": entry.get("decision_cycle_id"),
        "causal_episode_id": entry.get("causal_episode_id"),
        "entry_ts": entry.get("ts"),
        "entry_time_utc": _utc(entry["ts"]),
        "exit_ts": exit_row.get("ts"),
        "exit_time_utc": _utc(exit_row["ts"]),
        "side": entry.get("side"),
        "entry_price": entry.get("price"),
        "exit_price": exit_row.get("exit_price"),
        "qty_btc": exit_row.get("qty_btc"),
        "entry_mode": entry.get("entry_mode"),
        "phase": entry.get("phase"),
        "edge_class": entry.get("edge_class"),
        "execution_style": costs.get("execution_style"),
        "commission_verified": costs.get("commission_verified"),
        "total_cost_bps": costs.get("total_cost_bps"),
        "minimum_net_edge_bps": costs.get("minimum_net_edge_bps"),
        "gross_pnl_bps": exit_row.get("gross_pnl_bps"),
        "net_pnl_bps": exit_row.get("net_pnl_bps"),
        "net_pnl_usdt": exit_row.get("net_pnl_usdt"),
        "net_pnl_r": exit_row.get("net_pnl_r"),
        "holding_time_seconds": exit_row.get("holding_time_seconds"),
        "time_to_positive_net_seconds": exit_row.get(
            "time_to_positive_net_seconds"
        ),
        "economic_contract_version": thesis.get("economic_contract_version"),
        "flow_efficiency_state": economic.get("flow_efficiency_state"),
        "flow_efficiency": flow_efficiency,
        "execution_urgency_status": execution_urgency.get("status"),
        "execution_urgency_authority": execution_urgency.get("authority"),
        "oi_verification_status": oi_verification.get("status"),
        "consumed_band": economic.get("consumed_band"),
        "exit_reason": exit_row.get("risk_reason") or guardian.get("reason"),
        "guardian_version": guardian.get("version"),
        "guardian_exit_profile": guardian.get("exit_profile"),
        "guardian_trend_shield_active": guardian.get("trend_shield_active"),
        "proposer": thesis.get("proposer"),
        "proof_type": thesis.get("proof_type"),
        "impulse_phase": thesis.get("impulse_phase"),
        "primary_cash_anchor": thesis.get("primary_cash_anchor"),
        "oi_intent": oi.get("intent"),
        "oi_causal_class": oi.get("causal_class"),
        "bias_side": bias.get("context_side") or bias.get("direction"),
        "bias_phase": bias.get("phase"),
    }


def export(source):
    entries = {}
    completed = []
    with Path(source).open("r", encoding="utf-8") as handle:
        for raw in handle:
            try:
                row = json.loads(raw)
            except (TypeError, ValueError):
                continue
            event = str(row.get("event") or "")
            cycle_id = str(row.get("cycle_id") or "")
            if event == "ENTRY" and cycle_id:
                entries[cycle_id] = row
            elif event == "EXIT" and cycle_id in entries:
                completed.append(_compact(entries.pop(cycle_id), row))
    return sorted(completed, key=lambda row: (row["entry_ts"], row["cycle_id"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    args = parser.parse_args()
    rows = export(args.source)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    temporary.replace(output)
    print("exported %d sanitized shadow trades to %s" % (len(rows), output))


if __name__ == "__main__":
    main()
