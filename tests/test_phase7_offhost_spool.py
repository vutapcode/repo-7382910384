import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from recorder.offhost_spool import OffhostSpool
from recorder.offhost_integration import enqueue_closed_reference, enabled


class Backend:
    def __init__(self, status="ACKNOWLEDGED", head_sha="sha"):
        self.status=status; self.head_sha=head_sha; self.calls=0
    def put_if_absent(self, artifact, manifest): self.calls+=1; return {"status":self.status}
    def head(self, artifact_id): return {"sha256":self.head_sha}

class OffhostSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.art=self.root/"a"; self.art.write_bytes(b"payload")
        self.man={"artifact_id":"a"*64,"byte_size":7,"sha256":"sha"}
        self.mp=self.root/"m.json"; self.mp.write_text(json.dumps(self.man),encoding="utf-8")
    def tearDown(self): self.temp.cleanup()

    def test_disabled_flag_changes_nothing(self):
        spool=OffhostSpool(self.root/"spool")
        result=enqueue_closed_reference(spool,self.art,self.mp,self.man,env={})
        self.assertFalse(result["enqueued"]); self.assertEqual(spool.records,{})

    def test_queue_full_alarm_local_artifact_survives(self):
        spool=OffhostSpool(self.root/"spool",max_items=0)
        result=enqueue_closed_reference(spool,self.art,self.mp,self.man,env={"WSTRADE_OFFHOST_DURABILITY_ENABLED":"true"})
        self.assertFalse(result["enqueued"]); self.assertTrue(self.art.exists())
        self.assertIn("QUEUE_FULL", spool.alarm)

    def test_upload_failure_nonblocking_and_retry_no_delete(self):
        spool=OffhostSpool(self.root/"spool",retry_base=0.01,retry_cap=0.02)
        spool.enqueue(self.art,self.mp,self.man)
        rec=asyncio.run(spool.upload_once(Backend(status="RETRYABLE_FAILURE"),self.man["artifact_id"],now=1.0))
        self.assertEqual(rec.state,"RETRYABLE_FAILURE"); self.assertTrue(self.art.exists())
        self.assertGreater(rec.next_attempt_at,1.0)

    def test_ack_checksum_mismatch_corrupt_no_delete(self):
        spool=OffhostSpool(self.root/"spool"); spool.enqueue(self.art,self.mp,self.man)
        rec=asyncio.run(spool.upload_once(Backend(head_sha="different"),self.man["artifact_id"],now=1.0))
        self.assertEqual(rec.state,"CORRUPT"); self.assertTrue(self.art.exists()); self.assertEqual(spool.checksum_failures,1)

if __name__=="__main__": unittest.main()
