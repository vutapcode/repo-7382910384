import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "export_research_trade_history",
    ROOT / "ops" / "export_research_trade_history.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ResearchTradeExportTests(unittest.TestCase):
    def test_export_pairs_lifecycle_and_whitelists_fields(self):
        rows = [
            {
                "event": "ENTRY", "ts": 10.0, "cycle_id": "shadow:1",
                "decision_cycle_id": "decision:1", "causal_episode_id": "ign:1",
                "side": "LONG", "price": 100.0, "qty_btc": 0.001,
                "entry_mode": "IGNITION", "phase": "RELEASE",
                "edge_class": "BOOTSTRAP_UNVERIFIED",
                "api_secret": "must-not-leak", "balance_usdt": 5000.0,
                "entry_causal_thesis": {
                    "proposer": "binance_spot", "proof_type": "METAORDER_CONTINUATION",
                    "bias_thesis": {"context_side": "LONG", "phase": "ESTABLISHED_TREND"},
                },
            },
            {
                "event": "EXIT", "ts": 12.0, "cycle_id": "shadow:1",
                "exit_price": 101.0, "qty_btc": 0.001,
                "net_pnl_bps": 90.0, "net_pnl_usdt": 0.9,
                "holding_time_seconds": 2.0, "risk_reason": "THESIS_BREAK",
                "order_id": 12345,
                "execution_cost_model": {
                    "execution_style": "TAKER", "total_cost_bps": 10.0,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "events.jsonl"
            source.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            exported = MODULE.export(source)
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["cycle_id"], "shadow:1")
        self.assertEqual(exported[0]["proof_type"], "METAORDER_CONTINUATION")
        serialized = json.dumps(exported[0])
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("api_secret", serialized)
        self.assertNotIn("balance_usdt", serialized)
        self.assertNotIn("order_id", serialized)

    def test_incomplete_entry_is_not_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "events.jsonl"
            source.write_text(
                json.dumps({"event": "ENTRY", "ts": 10, "cycle_id": "open"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(MODULE.export(source), [])


if __name__ == "__main__":
    unittest.main()
