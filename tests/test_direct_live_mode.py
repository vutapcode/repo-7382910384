import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DirectLiveModeTests(unittest.TestCase):
    def _preflight(self, credential_dir):
        env = dict(os.environ)
        env.update({
            'WSTRADE_MODE': 'DIRECT_LIVE',
            'CREDENTIALS_DIRECTORY': str(credential_dir),
            'SMC_ENABLE_TRADING': 'false',
            'SMC_MAINNET_ARMED': 'false',
            'SMC_MAINNET_EXCLUSIVE_ACCOUNT': 'false',
        })
        return subprocess.run(
            [str(ROOT / '.venv' / 'bin' / 'python'),
             str(ROOT / 'ops' / 'mainnet_preflight.py')],
            cwd=ROOT, env=env, capture_output=True, text=True, check=False,
        )

    def test_direct_live_preflight_requires_both_systemd_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self._preflight(Path(temp))
        self.assertEqual(result.returncode, 2)
        self.assertIn('requires both Binance systemd credentials', result.stderr)

    def test_direct_live_preflight_accepts_nonempty_pair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'binance_api_key').write_text('key\n', encoding='utf-8')
            (root / 'binance_api_secret').write_text('secret\n', encoding='utf-8')
            result = self._preflight(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mode=DIRECT_LIVE', result.stdout)

    def test_checked_in_unit_uses_auto_promotion(self):
        unit = (ROOT / 'ops/systemd/wstrade-bot.service').read_text(encoding='utf-8')
        launcher = (ROOT / 'mainnet_tier_s_shadow_launcher.py').read_text(
            encoding='utf-8'
        )
        self.assertIn('Environment=WSTRADE_MODE=AUTO_PROMOTE', unit)
        self.assertIn('SetCredential=binance_api_key:', unit)
        self.assertIn('SetCredential=binance_api_secret:', unit)
        self.assertIn('if DIRECT_LIVE:', launcher)
        self.assertIn('"REPLAY", "72H_SOAK"', launcher)
        self.assertIn('await _promote_live(snapshot)', launcher)
        self.assertIn('if not LIVE_CAPABLE:', launcher)
        self.assertIn('mainnet_safety.exchange_entry_gate', (
            ROOT / '3_thuc_thi/wstrade_live_execution.py'
        ).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
