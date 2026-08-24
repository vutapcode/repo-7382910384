import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loi_he_thong import ops_supervisor
from loi_he_thong import ops_supervisor_safe
from loi_he_thong.runtime_lock import (
    DuplicateInstanceError,
    acquire_runtime_lock,
)


class RuntimeOperationsTests(unittest.TestCase):
    def test_singleton_lock_rejects_second_instance_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            first = acquire_runtime_lock('bot-test', runtime_dir=temp)
            try:
                with self.assertRaises(DuplicateInstanceError) as raised:
                    acquire_runtime_lock('bot-test', runtime_dir=temp)
                self.assertIn('DUPLICATE_INSTANCE', str(raised.exception))
            finally:
                first.close()
            second = acquire_runtime_lock('bot-test', runtime_dir=temp)
            second.close()

    def test_external_health_does_not_call_stale_file_running(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bot_path = root / 'bot.json'
            recorder_path = root / 'recorder.json'
            now = time.time()
            bot_path.write_text(json.dumps({
                'updated_at_ms': int(now * 1000), 'system_ready': True,
            }))
            recorder_path.write_text(json.dumps({
                'updated_at_ms': int((now - 30) * 1000),
                'current_status': 'OK',
            }))
            state = lambda unit: {
                'unit': unit, 'active_state': 'active', 'sub_state': 'running',
                'pid': 123, 'restarts': 0, 'query_error': None,
            }
            with (
                patch.object(ops_supervisor, 'BOT_HEARTBEAT', bot_path),
                patch.object(ops_supervisor, 'RECORDER_HEALTH', recorder_path),
                patch.object(ops_supervisor, '_service_state', side_effect=state),
                patch.object(ops_supervisor, '_process_resources', return_value={
                    'rss_bytes': 1, 'cpu_percent': 1.0,
                }),
            ):
                snapshot = ops_supervisor.build_snapshot(now)
            self.assertEqual(snapshot['recorder']['classification'], 'STALE_PROCESS')
            self.assertEqual(snapshot['status'], 'ERROR')

    def test_external_health_distinguishes_process_down(self):
        down = {
            'unit': 'x', 'active_state': 'inactive', 'sub_state': 'dead',
            'pid': 0, 'restarts': 0, 'query_error': None,
        }
        with (
            patch.object(ops_supervisor, '_service_state', return_value=down),
            patch.object(ops_supervisor, '_process_resources', return_value={
                'rss_bytes': 0, 'cpu_percent': 0.0,
            }),
        ):
            snapshot = ops_supervisor.build_snapshot(time.time())
        self.assertEqual(snapshot['bot']['classification'], 'PROCESS_DOWN')
        self.assertEqual(snapshot['recorder']['classification'], 'PROCESS_DOWN')

    def test_stall_recovery_dumps_then_kills_for_systemd_restart(self):
        with (
            patch.object(ops_supervisor.os, 'kill') as kill,
            patch.object(ops_supervisor.time, 'sleep'),
            patch.object(
                ops_supervisor_safe,
                '_bot_pid_still_current',
                side_effect=(True, True),
            ),
        ):
            self.assertTrue(ops_supervisor_safe._restart_stalled_bot_safe(123))
        self.assertEqual(kill.call_count, 2)
        self.assertEqual(kill.call_args_list[0].args, (123, ops_supervisor.signal.SIGUSR1))
        self.assertEqual(kill.call_args_list[1].args, (123, ops_supervisor.signal.SIGKILL))


if __name__ == '__main__':
    unittest.main()
