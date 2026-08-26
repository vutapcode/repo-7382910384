import json
from pathlib import Path
import tempfile
import unittest

from ops import trade_audit_mirror as audit
from ops.rebase_shadow_balance import last_position_event_seq


class TradeAuditMirrorTests(unittest.TestCase):
    def test_capital_audit_never_owns_position_sequence(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "events.jsonl"
            journal.write_text("".join([
                json.dumps({"event": "ENTRY", "event_seq": 7}) + "\n",
                json.dumps({
                    "event": "SHADOW_CAPITAL_ADJUSTMENT", "event_seq": 99,
                }) + "\n",
            ]))
            self.assertEqual(last_position_event_seq(journal), 7)

    def test_mirror_builds_complete_human_readable_trade(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "events.jsonl"
            rows = [
                {
                    "ts": 100.0, "event": "DECISION_EVALUATED",
                    "cycle_id": "decision:1", "decision": "GO",
                    "reason": "CAUSAL_PRICE_FLOW_QUORUM", "side": "LONG",
                    "entry_mode": "NORMAL", "phase": "ACCEPTANCE",
                    "decision_record": {
                        "strategy_code_version": "code1",
                        "strategy_config_version": "config1",
                        "inputs": {
                            "bias": {"direction": "LONG", "confidence": 0.8},
                            "s1_price_quorum": {"status": "PASS"},
                            "s2_executed_flow_quorum": {"status": "PASS"},
                            "oi_intent": {"regime": "NEW_LONG_BUILD"},
                        },
                        "output": {"edge_class": "HIGH_EDGE", "confidence": 0.7},
                    },
                },
                {
                    "ts": 101.0, "event": "ENTRY", "cycle_id": "shadow:1",
                    "decision_cycle_id": "decision:1", "side": "LONG",
                    "price": 100.0, "actual_qty_btc": 0.001,
                    "entry_mode": "NORMAL", "phase": "ACCEPTANCE",
                    "edge_class": "HIGH_EDGE", "hard_sl": 99.5,
                },
                {
                    "ts": 102.0, "event": "POSITION_STATE", "cycle_id": "shadow:1",
                    "price": 100.2, "best_r": 0.4,
                    "guardian_state": {"decision": "HOLD", "reason": "SUPPORT"},
                },
                {
                    "ts": 103.0, "event": "EXIT", "cycle_id": "shadow:1",
                    "side": "LONG", "entry_price": 100.0, "exit_price": 100.3,
                    "net_pnl_usdt": 0.01, "net_pnl_bps": 3.0,
                    "risk_reason": "SUPPORT_WITHDRAWN", "balance_usdt": 5000.01,
                },
            ]
            source.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            mirror = audit.AuditMirror(source, root / "out")
            self.assertEqual(mirror.run_once(), 4)
            trade = json.loads((root / "out" / "trades.jsonl").read_text())
            self.assertEqual(trade["status"], "CLOSED")
            self.assertEqual(trade["entry"]["basis"]["bias"]["direction"], "LONG")
            self.assertEqual(len(trade["timeline"]), 1)
            self.assertEqual(trade["exit"]["reason"], "SUPPORT_WITHDRAWN")
            latest = (root / "out" / "latest_trade.txt").read_text()
            self.assertIn("CAN CU VAO LENH", latest)
            self.assertIn("DIEN BIEN", latest)
            self.assertIn("KET QUA", latest)

    def test_reprocessing_checkpoint_does_not_duplicate_trade(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "events.jsonl"
            source.write_text(
                json.dumps({"ts": 1, "event": "ENTRY", "cycle_id": "t1"}) + "\n"
                + json.dumps({"ts": 2, "event": "EXIT", "cycle_id": "t1"}) + "\n"
            )
            output = root / "out"
            first = audit.AuditMirror(source, output)
            first.run_once()
            second = audit.AuditMirror(source, output)
            self.assertEqual(second.run_once(), 0)
            self.assertEqual(len((output / "trades.jsonl").read_text().splitlines()), 1)

    def test_mirror_consumes_live_lifecycle_and_commission_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "events.jsonl"
            rows = [
                {
                    "ts": 1.0, "event": "LIVE_ENTRY",
                    "position_cycle_id": "live:LONG:1",
                    "decision_cycle_id": "decision:live:1",
                    "causal_episode_id": "ign:spot:LONG:1",
                    "side": "LONG", "qty_btc": 0.001, "price": 100.0,
                    "proof_type": "FAILED_REVERSION",
                    "proposer": "binance_spot", "regime": "NORMAL",
                    "frozen_cost_contract": {"total_cost_bps": 8.5},
                },
                {
                    "ts": 1.1, "event": "LIVE_ORDER_UPDATE",
                    "clientOrderId": "ws_entry_1", "orderId": 7,
                    "executionType": "TRADE", "status": "FILLED",
                    "commissionAmount": "0.02", "commissionAsset": "USDT",
                    "commissionByAssetCumulative": {"USDT": 0.02},
                },
                {
                    "ts": 2.0, "event": "LIVE_EXIT",
                    "position_cycle_id": "live:LONG:1",
                    "decision_cycle_id": "decision:live:1",
                    "causal_episode_id": "ign:spot:LONG:1",
                    "side": "LONG", "entry_price": 100.0,
                    "exit_price": 100.2, "gross_pnl_bps": 20.0,
                    "fee_bps": 7.0, "net_pnl_bps": 13.0,
                    "reason": "TIER_S_PRICE_PLUS_CAUSE_EXIT",
                    "commission_source": "BINANCE_ORDER_TRADE_UPDATE",
                },
            ]
            source.write_text("".join(json.dumps(row) + "\n" for row in rows))
            mirror = audit.AuditMirror(source, root / "out")
            self.assertEqual(mirror.run_once(), 3)
            trade = json.loads((root / "out" / "trades.jsonl").read_text())
            self.assertFalse(trade["virtual_only"])
            self.assertEqual(trade["trade_id"], "live:LONG:1")
            self.assertEqual(trade["entry"]["proof_type"], "FAILED_REVERSION")
            self.assertEqual(trade["exit"]["net_pnl_bps"], 13.0)
            activity = (root / "out" / "activity.jsonl").read_text()
            self.assertIn('"event":"LIVE_ORDER_UPDATE"', activity)


if __name__ == "__main__":
    unittest.main()
