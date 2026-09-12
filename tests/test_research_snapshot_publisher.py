import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


path = Path(__file__).parents[1] / "ops" / "publish_research_snapshot.py"
spec = importlib.util.spec_from_file_location("research_publisher", path)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class ResearchPublisherTests(unittest.TestCase):
    def test_publish_rewrites_polluted_branch_as_evidence_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote = root / "remote.git"
            seed = root / "seed"
            clone = root / "publisher"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(["git", "init", "-b", "telemetry", str(seed)],
                           check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.name", "test"], cwd=seed,
                           check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"],
                           cwd=seed, check=True)
            (seed / "tests").mkdir()
            (seed / "research_live").mkdir()
            (seed / "main.py").write_text("print('pollution')\n", encoding="utf-8")
            (seed / "tests" / "test_x.py").write_text("pass\n", encoding="utf-8")
            (seed / "research_live" / "timeline.json").write_text(
                "[]\n", encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=seed, check=True)
            subprocess.run(["git", "commit", "-m", "polluted"], cwd=seed,
                           check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "remote", "add", "origin", str(remote)],
                           cwd=seed, check=True)
            subprocess.run(["git", "push", "origin", "telemetry"], cwd=seed,
                           check=True, stdout=subprocess.DEVNULL)

            journal = root / "events.jsonl"
            journal.write_text(json.dumps({
                "event": "DECISION_EVALUATED", "ts": 1000.0,
                "run_id": "run-1", "runtime_commit": "d" * 40,
                "code_version": "code", "config_version": "config",
                "runtime_mode": "SHADOW", "source_branch": "main",
            }) + "\n", encoding="utf-8")
            bot = root / "bot.json"
            bot.write_text(json.dumps({
                "run_id": "run-1", "runtime_commit": "d" * 40,
                "code_version": "code", "config_version": "config",
                "runtime_mode": "SHADOW", "source_branch": "main",
                "runtime_execution_mode": "SHADOW_DEMO",
            }), encoding="utf-8")
            recorder = root / "recorder.json"
            recorder.write_text(json.dumps({
                "run_id": "rec-1", "runtime_commit": "d" * 40,
                "code_version": "rec-code", "config_version": "rec-config",
                "runtime_mode": "SHADOW", "source_branch": "main",
            }), encoding="utf-8")
            originals = {
                name: getattr(publisher, name) for name in (
                    "JOURNAL", "RUNTIME_STATE", "TRADE_AUDIT",
                    "OPPORTUNITY_WAL", "FEATURE_WAL", "BOT_HEALTH",
                    "RECORDER_HEALTH", "PUBLISH_STATE", "CLONE", "REMOTE",
                )
            }
            old_service_state = publisher._service_state
            try:
                publisher.JOURNAL = journal
                publisher.RUNTIME_STATE = root / "runtime.json"
                publisher.TRADE_AUDIT = root / "trades.jsonl"
                publisher.OPPORTUNITY_WAL = root / "opportunities"
                publisher.FEATURE_WAL = root / "features"
                publisher.BOT_HEALTH = bot
                publisher.RECORDER_HEALTH = recorder
                publisher.PUBLISH_STATE = root / "state.json"
                publisher.CLONE = clone
                publisher.REMOTE = str(remote)
                publisher._service_state = lambda _name: "active"
                publisher._publish()
            finally:
                for name, value in originals.items():
                    setattr(publisher, name, value)
                publisher._service_state = old_service_state
            tree = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only",
                 "telemetry"], check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.splitlines()
            publisher._assert_evidence_tree(tree)
            self.assertTrue(tree)
            self.assertTrue(all(path.startswith("telemetry/") for path in tree))
            self.assertIn("telemetry/manifest.json", tree)
            self.assertIn("telemetry/decisions/timeline.jsonl", tree)
            self.assertIn("telemetry/runtime/heartbeat.jsonl", tree)
            self.assertNotIn("main.py", tree)

    def test_runtime_summary_separates_research_from_live_like(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / "runtime.json"
            runtime.write_text(json.dumps({
                "trades": 20, "wins": 0, "losses": 20,
                "shadow_ledgers": {
                    "research_probe": {
                        "trades": 20, "wins": 0, "losses": 20,
                        "realized_pnl": -2.0,
                    },
                    "live_like": {
                        "trades": 0, "wins": 0, "losses": 0,
                        "realized_pnl": 0.0,
                    },
                    "private_account": {"secret": "must-not-leak"},
                },
            }), encoding="utf-8")
            old = publisher.RUNTIME_STATE
            publisher.RUNTIME_STATE = runtime
            try:
                summary = publisher._runtime_summary()
            finally:
                publisher.RUNTIME_STATE = old
        self.assertEqual(summary["top_level_demo_scope"], "INCLUDES_RESEARCH_PROBE")
        self.assertEqual(summary["promotion_metric_scope"], "LIVE_LIKE_SHADOW_ONLY")
        self.assertEqual(summary["shadow_ledgers"]["research_probe"]["trades"], 20)
        self.assertEqual(summary["shadow_ledgers"]["live_like"]["trades"], 0)
        self.assertNotIn("must-not-leak", str(summary))

    def test_allowlist_never_exports_unknown_or_secret_fields(self):
        row = {
            "event": "ENTRY", "ts": 1.0, "cycle_id": "c1", "side": "LONG",
            "run_id": "run-1", "runtime_commit": "a" * 40,
            "code_version": "code-1", "config_version": "config-1",
            "price": 100.0, "api_key": "secret", "account": {"balance": 5},
            "entry_causal_thesis": {"proof_type": "PERSISTENT_METAORDER"},
        }
        compact = publisher._compact_event(row)
        self.assertEqual(compact["price"], 100.0)
        self.assertEqual(compact["run_id"], "run-1")
        self.assertEqual(compact["runtime_commit"], "a" * 40)
        self.assertEqual(compact["identity_status"], "COMPLETE")
        self.assertNotIn("api_key", compact)
        self.assertNotIn("account", compact)
        self.assertNotIn("secret", str(compact))

    def test_legacy_event_exposes_missing_identity_without_faking_it(self):
        compact = publisher._compact_event({"event": "ENTRY", "ts": 1.0})
        self.assertEqual(compact["identity_status"], "LEGACY_MISSING_IDENTITY")
        for name in publisher.IDENTITY_FIELDS:
            self.assertIn(name, compact)
            self.assertIsNone(compact[name])

    def test_evidence_tree_rejects_source_tests_and_docs(self):
        publisher._assert_evidence_tree([
            "telemetry/manifest.json",
            "telemetry/decisions/timeline.jsonl",
        ])
        for path in (
            "main.py", "telemetry/tests/test_x.py", "telemetry/README.md",
        ):
            with self.assertRaises(RuntimeError):
                publisher._assert_evidence_tree([path])

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

    def test_opportunity_history_is_readable_and_strictly_sanitized(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "opportunity_dossier" / "2026-08-30"
            root.mkdir(parents=True)
            (root / "17.jsonl").write_text(json.dumps({
                "event_time_ms": 2_000,
                "payload": {
                    "version": "OPPORTUNITY_DOSSIER_V1_CAUSAL_TIMELINE",
                    "cycle_id": "c1", "causal_episode_id": "episode-1",
                    "side": "LONG", "decision_count": 3,
                    "why_no_entry": {
                        "primary_reason": "WAIT_CURRENT_CASH_CONVERSION",
                        "origin_reason": "WAIT_OI_REFRESH",
                        "terminal_reason": "WAIT_CURRENT_CASH_CONVERSION",
                        "all_reasons": ["BIAS_NOT_READY"],
                        "private_account": "must-not-leak",
                    },
                    "what_happened_after": {
                        "max_favorable_excursion_bps": 12.0,
                        "windows": [{
                            "window_seconds": 5, "signed_close_bps": 4.0,
                            "secret": "must-not-leak",
                        }],
                    },
                    "classification": "MISS_SCREEN_ONLY",
                    "api_secret": "must-not-leak",
                },
            }) + "\n", encoding="utf-8")
            old = publisher.OPPORTUNITY_WAL
            publisher.OPPORTUNITY_WAL = root.parent
            try:
                rows = publisher._opportunity_history(0.0)
            finally:
                publisher.OPPORTUNITY_WAL = old
        self.assertEqual(rows[0]["causal_episode_id"], "episode-1")
        self.assertEqual(
            rows[0]["why_no_entry"]["primary_reason"],
            "WAIT_CURRENT_CASH_CONVERSION",
        )
        self.assertEqual(
            rows[0]["why_no_entry"]["origin_reason"], "WAIT_OI_REFRESH",
        )
        self.assertEqual(
            rows[0]["why_no_entry"]["terminal_reason"],
            "WAIT_CURRENT_CASH_CONVERSION",
        )
        self.assertEqual(
            rows[0]["what_happened_after"]["windows"][0]["signed_close_bps"],
            4.0,
        )
        self.assertNotIn("must-not-leak", str(rows))

    def test_rotated_zero_cursor_uses_bounded_backfill(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "events.jsonl"
            historical = "".join(
                json.dumps({
                    "event": "DECISION_EVALUATED", "cycle_id": str(i),
                    "padding": "x" * 40,
                }) + "\n" for i in range(20)
            )
            journal.write_text(historical, encoding="utf-8")
            old_stat = journal.stat()
            segment = publisher.journal_segments.prepare_append(
                journal, max_bytes=1,
            )
            journal.write_text(
                json.dumps({"event": "ENTRY", "cycle_id": "new"}) + "\n",
                encoding="utf-8",
            )
            old_journal = publisher.JOURNAL
            old_tail = publisher.INITIAL_TAIL_BYTES
            publisher.JOURNAL = journal
            publisher.INITIAL_TAIL_BYTES = 240
            try:
                rows, next_checkpoint = publisher._journal_delta({
                    "device": old_stat.st_dev,
                    "inode": old_stat.st_ino,
                    "offset": 0,
                })
            finally:
                publisher.JOURNAL = old_journal
                publisher.INITIAL_TAIL_BYTES = old_tail
            current_inode = journal.stat().st_ino
            segment_exists = segment.exists()
        ids = [row.get("cycle_id") for row in rows]
        self.assertIn("new", ids)
        self.assertNotIn("0", ids)
        self.assertLess(len(rows), 20)
        self.assertEqual(next_checkpoint["inode"], current_inode)
        self.assertTrue(segment_exists)


if __name__ == "__main__":
    unittest.main()
