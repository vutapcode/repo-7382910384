import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'ops' / 'shadow_journal_recovery_guard.py'


class ShadowJournalRecoveryGuardTests(unittest.TestCase):
    def run_guard(self, directory, rows):
        path = Path(directory) / 'events.jsonl'
        path.write_text(''.join(rows), encoding='utf-8')
        env = dict(os.environ)
        env['SMC_JOURNAL_DIR'] = str(directory)
        env['SMC_SHADOW_EVENTS_PATH'] = str(path)
        return subprocess.run(
            [str(ROOT / '.venv' / 'bin' / 'python'), str(GUARD)],
            cwd=ROOT, env=env, capture_output=True, text=True, check=False,
        )

    def test_bootstrap_only_journal_does_not_require_position_state(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_guard(temp, [
                '{"event":"PRIVATE_USER_STREAM_CONNECTED"}\n',
                '{"event":"WHALE_INTENT"}\n',
            ])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_trade_journal_without_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_guard(temp, ['{"event":"ENTRY"}\n'])
        self.assertEqual(result.returncode, 1)
        self.assertIn('missing_state_with_stateful_journal', result.stderr)

    def test_corrupt_journal_without_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_guard(temp, ['not-json\n'])
        self.assertEqual(result.returncode, 1)
        self.assertIn('journal_corrupt_line', result.stderr)


if __name__ == '__main__':
    unittest.main()
