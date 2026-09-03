import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from recorder import offhost_durability as d


class OffhostDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _wal(self, hour="01", rows=2):
        path = self.root / "raw" / "wal" / "agg_trade" / "2026-09-03" / f"{hour}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for index in range(rows):
                handle.write(json.dumps({
                    "schema_version": 3, "code_version": "code", "config_version": "cfg",
                    "event_contract_version": "evt", "available_time_ms": 1000 + index,
                    "payload": {"n": index},
                }) + "\n")
        return path

    def test_current_open_wal_not_sealed(self):
        path = self._wal("11")
        now = datetime(2026, 9, 3, 11, 20, tzinfo=timezone.utc)
        with self.assertRaisesRegex(d.DurabilityError, "CURRENT_OPEN_WAL_NOT_SEALABLE"):
            d.build_manifest(path, self.root, now=now)

    def test_closed_segment_seal_deterministic_and_streaming_checksum(self):
        path = self._wal("01")
        now = datetime(2026, 9, 3, 11, 20, tzinfo=timezone.utc)
        a = d.build_manifest(path, self.root, now=now, canonical_replay_hash="replay")
        b = d.build_manifest(path, self.root, now=now, canonical_replay_hash="replay")
        self.assertEqual(a, b)
        self.assertEqual(a["row_count"], 2)
        self.assertEqual(a["byte_size"], path.stat().st_size)
        self.assertEqual(a["sha256"], d.scan_jsonl(path, chunk_size=7)["sha256"])

    def test_manifest_atomic_and_immutable(self):
        path = self._wal("01")
        manifest = d.build_manifest(path, self.root, now=datetime(2026, 9, 3, 11, tzinfo=timezone.utc))
        target = self.root / "spool" / "manifest.json"
        d.atomic_write_manifest(target, manifest)
        d.atomic_write_manifest(target, manifest)
        changed = dict(manifest); changed["row_count"] = 99
        with self.assertRaisesRegex(d.DurabilityError, "IMMUTABLE_MANIFEST_CONFLICT"):
            d.atomic_write_manifest(target, changed)

    def test_sanitized_github_telemetry_is_not_wal_backup(self):
        self.assertFalse(d.SANITIZED_RESEARCH_TELEMETRY_IS_WAL_BACKUP)

    def test_manifest_contains_no_secrets(self):
        path = self._wal("01")
        manifest = d.build_manifest(path, self.root, now=datetime(2026, 9, 3, 11, tzinfo=timezone.utc))
        text = json.dumps(manifest).upper()
        for marker in d.SECRET_MARKERS:
            self.assertNotIn(marker, text)

    def test_symlink_escape_rejected(self):
        outside = self.root.parent / (self.root.name + "-outside.jsonl")
        outside.write_text("{}\n", encoding="utf-8")
        link = self.root / "raw" / "wal" / "agg_trade" / "2026-09-03" / "01.jsonl"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        try:
            with self.assertRaises(d.DurabilityError):
                d.build_manifest(link, self.root, now=datetime(2026, 9, 3, 11, tzinfo=timezone.utc))
        finally:
            outside.unlink(missing_ok=True)

if __name__ == "__main__":
    unittest.main()
