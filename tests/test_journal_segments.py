import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from loi_he_thong import journal_segments
from recorder.decision_tap import DecisionTap
from ops import trade_audit_mirror


class JournalSegmentTests(unittest.TestCase):
    def test_cursor_drains_old_inode_then_current(self):
        with tempfile.TemporaryDirectory() as temp:
            current = Path(temp) / "events.jsonl"
            current.write_text('{"event":"A"}\n', encoding="utf-8")
            stat = current.stat()
            offset = stat.st_size
            segment = journal_segments.prepare_append(
                current, max_bytes=1, max_segments=8,
            )
            current.write_text('{"event":"B"}\n', encoding="utf-8")
            planned = journal_segments.cursor_sources(
                current, stat.st_dev, stat.st_ino, offset,
            )
            self.assertEqual(planned, [(segment, offset), (current, 0)])

    def test_cursor_past_rotated_eof_does_not_replay_segment_from_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            current = Path(temp) / "events.jsonl"
            current.write_text('{"event":"A"}\n', encoding="utf-8")
            stat = current.stat()
            segment = journal_segments.prepare_append(
                current, max_bytes=1, max_segments=8,
            )
            current.write_text('{"event":"B"}\n', encoding="utf-8")
            planned = journal_segments.cursor_sources(
                current, stat.st_dev, stat.st_ino, stat.st_size + 100,
            )
            self.assertEqual(
                planned, [(segment, segment.stat().st_size), (current, 0)],
            )

    def test_decision_tap_does_not_lose_rollover_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "events.jsonl"
            current.write_text('{"event":"A","ts":1}\n', encoding="utf-8")
            config = SimpleNamespace(
                journal_events_path=current,
                journal_cycles_path=root / "cycles.json",
                data_root=root,
                cycles_snapshot_interval=10.0,
                decision_poll_interval=0.1,
            )
            tap = DecisionTap(config, lambda *args, **kwargs: None)
            rows, tap.offset = tap._read_new_events()
            self.assertEqual([row["event"] for row in rows], ["A"])
            journal_segments.prepare_append(current, max_bytes=1)
            current.write_text('{"event":"B","ts":2}\n', encoding="utf-8")
            rows, tap.offset = tap._read_new_events()
            self.assertEqual([row["event"] for row in rows], ["B"])

    def test_operator_mirror_is_inode_aware(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "events.jsonl"
            current.write_text(
                json.dumps({"event": "DECISION_EVALUATED", "cycle_id": "d1"}) + "\n",
                encoding="utf-8",
            )
            mirror = trade_audit_mirror.AuditMirror(current, root / "out")
            self.assertEqual(mirror.run_once(), 1)
            journal_segments.prepare_append(current, max_bytes=1)
            current.write_text("".join((
                json.dumps({"event": "ENTRY", "cycle_id": "t1"}) + "\n",
                json.dumps({"event": "EXIT", "cycle_id": "t1"}) + "\n",
            )), encoding="utf-8")
            self.assertEqual(mirror.run_once(), 2)
            self.assertEqual(
                len((root / "out" / "trades.jsonl").read_text().splitlines()),
                1,
            )

    def test_latest_event_search_crosses_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            current = Path(temp) / "events.jsonl"
            current.write_text(
                json.dumps({"event": "ENTRY", "event_seq": 4}) + "\n",
                encoding="utf-8",
            )
            journal_segments.prepare_append(current, max_bytes=1)
            current.write_text(
                json.dumps({"event": "DECISION_EVALUATED"}) + "\n",
                encoding="utf-8",
            )
            row = journal_segments.last_matching_event(current, {"ENTRY", "EXIT"})
            self.assertEqual(row["event_seq"], 4)

    def test_unterminated_crash_tail_has_no_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            current = Path(temp) / "events.jsonl"
            current.write_bytes(
                b'{"event":"EXIT","event_seq":5}\n' + (b"\x00" * 50)
            )
            row = journal_segments.last_matching_event(current, {"ENTRY", "EXIT"})
            self.assertEqual(row["event_seq"], 5)

    def test_newline_terminated_corruption_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            current = Path(temp) / "events.jsonl"
            current.write_bytes(b'{"event":"EXIT"}\nnot-json\n')
            with self.assertRaises(json.JSONDecodeError):
                journal_segments.last_matching_event(current, {"ENTRY", "EXIT"})


if __name__ == "__main__":
    unittest.main()
