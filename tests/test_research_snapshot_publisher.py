import importlib.util
from pathlib import Path
import unittest


path = Path(__file__).parents[1] / "ops" / "publish_research_snapshot.py"
spec = importlib.util.spec_from_file_location("research_publisher", path)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class ResearchPublisherTests(unittest.TestCase):
    def test_allowlist_never_exports_unknown_or_secret_fields(self):
        row = {
            "event": "ENTRY", "ts": 1.0, "cycle_id": "c1", "side": "LONG",
            "price": 100.0, "api_key": "secret", "account": {"balance": 5},
            "entry_causal_thesis": {"proof_type": "PERSISTENT_METAORDER"},
        }
        compact = publisher._compact_event(row)
        self.assertEqual(compact["price"], 100.0)
        self.assertNotIn("api_key", compact)
        self.assertNotIn("account", compact)
        self.assertNotIn("secret", str(compact))

    def test_candidate_export_keeps_miss_reason_and_consumed(self):
        compact = publisher._compact_event({
            "event": "DECISION_EVALUATED", "ts": 2.0, "side": "SHORT",
            "decision": "WAIT", "reason": "WAIT_CHASE",
            "miss_taxonomy": "WAIT_CHASE", "failed_gates": ["WAIT_CHASE"],
            "impulse_consumed_fraction": 0.51,
            "causal_episode_id": "ign:spot:SHORT:1",
        })
        self.assertEqual(compact["miss_taxonomy"], "WAIT_CHASE")
        self.assertEqual(compact["consumed_fraction"], 0.51)
        self.assertEqual(compact["causal_episode_id"], "ign:spot:SHORT:1")

    def test_closed_trade_history_uses_second_allowlist(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "trades.jsonl"
            source.write_text(json.dumps({
                "cycle_id": "c1", "exit_ts": 100.0, "side": "LONG",
                "net_pnl_bps": 4.0, "api_secret": "must-not-leak",
            }) + "\n")
            old = publisher.TRADE_AUDIT
            publisher.TRADE_AUDIT = source
            try:
                rows = publisher._closed_trade_history(0.0)
            finally:
                publisher.TRADE_AUDIT = old
        self.assertEqual(rows[0]["net_pnl_bps"], 4.0)
        self.assertNotIn("api_secret", rows[0])
        self.assertNotIn("must-not-leak", str(rows[0]))


if __name__ == "__main__":
    unittest.main()
