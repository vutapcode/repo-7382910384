import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from recorder import offhost_durability as d
from recorder.offhost_backends.filesystem import FilesystemBackend, STATUS
from ops.restore_offhost_wal import restore_and_replay


class RestoreBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.data = self.root / "data"; self.backend = FilesystemBackend(self.root / "backend")
        self.wal = self.data / "raw/wal/agg_trade/2026-09-03/01.jsonl"
        self.wal.parent.mkdir(parents=True)
        self.wal.write_text(json.dumps({
            "schema_version":3,"code_version":"code","config_version":"cfg",
            "event_contract_version":"evt","available_time_ms":1,"payload":{}
        })+"\n", encoding="utf-8")
        self.replay_hash = hashlib.sha256(self.wal.read_bytes()).hexdigest()
        self.manifest = d.build_manifest(self.wal, self.data, now=datetime(2026,9,3,11,tzinfo=timezone.utc), canonical_replay_hash=self.replay_hash)

    def tearDown(self): self.temp.cleanup()

    def runner(self, restored_root):
        p = Path(restored_root) / self.manifest["source_relative_path"]
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def test_retry_idempotent_no_duplicate(self):
        first = self.backend.put_if_absent(self.wal, self.manifest)
        second = self.backend.put_if_absent(self.wal, self.manifest)
        self.assertEqual(first["status"], "ACKNOWLEDGED"); self.assertTrue(first["created"])
        self.assertEqual(second["status"], "ACKNOWLEDGED"); self.assertFalse(second["created"])
        self.assertEqual(len(list((self.root/"backend/artifacts").iterdir())), 1)

    def test_restore_hash_and_two_replays_deterministic(self):
        self.backend.put_if_absent(self.wal, self.manifest)
        report = restore_and_replay(self.backend, self.manifest["artifact_id"], replay_runner=self.runner,
                                    expected_versions={"code_version":"code","config_version":"cfg"})
        self.assertEqual(report["status"], "RESTORE_VERIFIED")
        self.assertFalse(report["production_copy_performed"])

    def test_version_mismatch_fail_closed(self):
        self.backend.put_if_absent(self.wal, self.manifest)
        report = restore_and_replay(self.backend, self.manifest["artifact_id"], replay_runner=self.runner,
                                    expected_versions={"code_version":"other"})
        self.assertEqual(report["status"], "VERSION_BOUNDARY_MISMATCH")

    def test_non_deterministic_replay_rejected(self):
        self.backend.put_if_absent(self.wal, self.manifest)
        counter = iter(["a","b"])
        report = restore_and_replay(self.backend, self.manifest["artifact_id"], replay_runner=lambda _: next(counter))
        self.assertEqual(report["status"], "REPLAY_NONDETERMINISTIC")

    def test_path_traversal_manifest_rejected(self):
        bad=dict(self.manifest); bad["source_relative_path"]="../../active/events.jsonl"
        aid=bad["artifact_id"]
        self.backend.put_if_absent(self.wal,bad)
        report=restore_and_replay(self.backend,aid,replay_runner=self.runner)
        self.assertEqual(report["status"],"MANIFEST_INVALID")

    def test_filesystem_backend_never_claims_offhost(self):
        self.assertEqual(STATUS, "FILESYSTEM_TEST_BACKEND_NOT_OFFHOST")

if __name__ == "__main__": unittest.main()
