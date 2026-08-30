import importlib.util
import json
from pathlib import Path
import tempfile
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

    def test_exit_export_keeps_guardian_recovery_path_without_unknown_fields(self):
        compact = publisher._compact_event({
            "event": "EXIT", "ts": 3.0, "side": "LONG",
            "guardian_state": {
                "reason": "TIER_S_PRICE_PLUS_CAUSE_EXIT",
                "guardian_phase": "FAILED_RECOVERY",
                "pullback_start_ms": 1000.0,
                "worst_adverse_bps": 4.2,
                "reclaim_fraction": 0.25,
                "recovery_conversion_state": "ABSENT",
                "opposing_flow_state": "PERSISTENT",
                "recovery_result": "FAILED",
                "failed_recovery_reason": "RECLAIM_LOST",
                "private_payload": "must-not-leak",
            },
        })
        recovery = compact["guardian"]
        self.assertEqual(recovery["guardian_phase"], "FAILED_RECOVERY")
        self.assertEqual(recovery["recovery_result"], "FAILED")
        self.assertNotIn("private_payload", recovery)

    def test_closed_trade_history_uses_second_allowlist(self):
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

    def test_checkpoint_boundary_does_not_skip_first_new_event(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "events.jsonl"
            first = json.dumps({"event": "DECISION_EVALUATED", "cycle_id": "a"}) + "\n"
            journal.write_text(first, encoding="utf-8")
            stat = journal.stat()
            checkpoint = {
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "offset": stat.st_size,
            }
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "ENTRY", "cycle_id": "b"}) + "\n")
            old = publisher.JOURNAL
            publisher.JOURNAL = journal
            try:
                rows, next_checkpoint = publisher._journal_delta(checkpoint)
            finally:
                publisher.JOURNAL = old
        self.assertEqual([row["cycle_id"] for row in rows], ["b"])
        self.assertGreater(next_checkpoint["offset"], checkpoint["offset"])


if __name__ == "__main__":
    unittest.main()
