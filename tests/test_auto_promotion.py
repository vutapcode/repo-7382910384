import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loi_he_thong.auto_promotion import PromotionController


def eligible_state():
    return SimpleNamespace(
        code_version='code', strategy_config_version='config',
        mainnet_shadow_balance_usdt=5.4, host_cpu_15m_pct=10.0,
        host_cpu_1h_pct=10.0, host_cpu_p95_pct=12.0,
        host_cpu_hard_limit_respected=True, canonical_opportunity_count=100,
        canonical_opportunity_qualified=100,
        canonical_opportunity_captured=80,
        mainnet_shadow_trades=30,
        mainnet_shadow_gross_profit=2.0, mainnet_shadow_gross_loss=1.0,
        mainnet_shadow_realized_pnl=1.0, mainnet_shadow_stress_25bps_pnl=0.2,
        lightsail_metric_fresh=True,
        host_cpu_snapshot={
            'max_window_pct': 12.0,
            'coverage_15m_complete': True,
            'coverage_1h_complete': True,
        },
        production_workload_blockers=[], shadow_integrity_fault=False,
        shadow_persistence_dirty=False, event_loop_stalled=False,
        guardian_latency_samples=1000, guardian_latency_p95_ms=55.0,
    )


class AutoPromotionTests(unittest.TestCase):
    @staticmethod
    def replay(path, code='code', config='config'):
        path.write_text(json.dumps({
            'passed': True, 'code_version': code, 'config_version': config,
            'strategy_authority': 'IGNITION_CORE_V1',
        }))

    def test_all_gates_make_auto_promotion_eligible(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            'WSTRADE_MODE': 'AUTO_PROMOTE', 'WSTRADE_VALIDATION_HOURS': '72',
            'WSTRADE_IGNITION_MANUAL_APPROVAL': 'true',
            'WSTRADE_REPLAY_REPORT_PATH': str(Path(temp) / 'replay.json'),
        }, clear=False):
            path = Path(temp) / 'promotion.json'
            (Path(temp) / 'replay.json').write_text(json.dumps({
                'passed': True, 'code_version': 'code', 'config_version': 'config',
                'strategy_authority': 'IGNITION_CORE_V1',
            }))
            path.write_text(json.dumps({
                'validation_started_at': 0.0, 'code_version': 'code',
                'config_version': 'config', 'cpu_violation_count': 0,
                'shadow_peak_balance': 5.4, 'max_shadow_drawdown_usdt': 0.0,
                'armed': False,
            }))
            result = PromotionController(path).evaluate(eligible_state(), now=73 * 3600)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['blockers'], ())

    def test_stale_external_metric_and_cpu_violation_fail_closed(self):
        state = eligible_state()
        state.lightsail_metric_fresh = False
        state.host_cpu_15m_pct = 30.0
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            'WSTRADE_MODE': 'AUTO_PROMOTE', 'WSTRADE_VALIDATION_HOURS': '0',
            'WSTRADE_REPLAY_REPORT_PATH': str(Path(temp) / 'replay.json'),
        }, clear=False):
            result = PromotionController(Path(temp) / 'promotion.json').evaluate(state, now=1)
        self.assertFalse(result['eligible'])
        self.assertIn('CPU_VALIDATION_FAILED', result['blockers'])
        self.assertIn('LIGHTSAIL_METRIC_STALE', result['blockers'])

    def test_ignition_never_auto_promotes_without_manual_approval(self):
        state = eligible_state()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            'WSTRADE_MODE': 'AUTO_PROMOTE', 'WSTRADE_VALIDATION_HOURS': '0',
            'WSTRADE_IGNITION_MANUAL_APPROVAL': 'false',
            'WSTRADE_REPLAY_REPORT_PATH': str(Path(temp) / 'replay.json'),
        }, clear=False):
            self.replay(Path(temp) / 'replay.json')
            result = PromotionController(Path(temp) / 'promotion.json').evaluate(
                state, now=1
            )
        self.assertFalse(result['eligible'])
        self.assertIn('IGNITION_MANUAL_APPROVAL_REQUIRED', result['blockers'])

    def test_cpu_fault_restarts_full_validation_epoch_and_evidence(self):
        state = eligible_state()
        state.host_cpu_15m_pct = 30.0
        state.host_cpu_hard_limit_respected = False
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            'WSTRADE_MODE': 'AUTO_PROMOTE', 'WSTRADE_VALIDATION_HOURS': '72',
            'WSTRADE_REPLAY_REPORT_PATH': str(Path(temp) / 'replay.json'),
        }, clear=False):
            report = Path(temp) / 'replay.json'
            self.replay(report)
            ledger = Path(temp) / 'promotion.json'
            ledger.write_text(json.dumps({
                'validation_started_at': 0.0, 'code_version': 'code',
                'config_version': 'config', 'armed': True,
            }))
            controller = PromotionController(ledger)
            failed = controller.evaluate(state, now=73 * 3600)
            self.assertEqual(failed['validation_hours'], 0.0)
            self.assertEqual(
                failed['validation_restart_reason'], 'CPU_SAFETY_VIOLATION'
            )

            state.host_cpu_15m_pct = 10.0
            state.host_cpu_hard_limit_respected = True
            state.canonical_opportunity_count += 100
            state.canonical_opportunity_qualified += 100
            state.canonical_opportunity_captured += 80
            state.mainnet_shadow_trades += 30
            state.mainnet_shadow_gross_profit += 2.0
            state.mainnet_shadow_gross_loss += 1.0
            state.mainnet_shadow_realized_pnl += 1.0
            state.mainnet_shadow_stress_25bps_pnl += 0.2
            still_soaking = controller.evaluate(state, now=74 * 3600)
        self.assertFalse(still_soaking['eligible'])
        self.assertEqual(still_soaking['opportunity_events'], 100)
        self.assertEqual(still_soaking['shadow_trades'], 30)
        self.assertIn('VALIDATION_HOURS_INCOMPLETE', still_soaking['blockers'])

    def test_p95_above_target_blocks_promotion_without_resetting_soak(self):
        state = eligible_state()
        state.host_cpu_p95_pct = 27.0
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            'WSTRADE_MODE': 'AUTO_PROMOTE', 'WSTRADE_VALIDATION_HOURS': '72',
            'WSTRADE_REPLAY_REPORT_PATH': str(Path(temp) / 'replay.json'),
        }, clear=False):
            report = Path(temp) / 'replay.json'
            self.replay(report)
            ledger = Path(temp) / 'promotion.json'
            ledger.write_text(json.dumps({
                'validation_started_at': 0.0, 'code_version': 'code',
                'config_version': 'config', 'armed': False,
            }))
            result = PromotionController(ledger).evaluate(
                state, now=73 * 3600
            )
        self.assertFalse(result['eligible'])
        self.assertEqual(result['validation_hours'], 73.0)
        self.assertIn('CPU_VALIDATION_FAILED', result['blockers'])
        self.assertNotEqual(
            result.get('validation_restart_reason'), 'CPU_SAFETY_VIOLATION'
        )

    def test_code_change_cannot_reuse_old_opportunities_or_trades(self):
        state = eligible_state()
        state.code_version = 'new-code'
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            'WSTRADE_MODE': 'AUTO_PROMOTE', 'WSTRADE_VALIDATION_HOURS': '0',
            'WSTRADE_REPLAY_REPORT_PATH': str(Path(temp) / 'replay.json'),
        }, clear=False):
            ledger = Path(temp) / 'promotion.json'
            ledger.write_text(json.dumps({
                'validation_started_at': 0.0, 'code_version': 'old-code',
                'config_version': 'config', 'armed': True,
                'opportunity_events': 100, 'shadow_trades': 30,
            }))
            result = PromotionController(ledger).evaluate(state, now=1.0)
        self.assertFalse(result['armed'])
        self.assertEqual(result['opportunity_events'], 0)
        self.assertEqual(result['shadow_trades'], 0)
        self.assertEqual(result['validation_restart_reason'], 'CODE_OR_CONFIG_CHANGED')

    def test_disk_change_cannot_relabel_running_process_evidence(self):
        state = eligible_state()
        state.runtime_project_root = '/runtime'
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            'WSTRADE_MODE': 'AUTO_PROMOTE', 'WSTRADE_VALIDATION_HOURS': '0',
            'WSTRADE_REPLAY_REPORT_PATH': str(Path(temp) / 'replay.json'),
        }, clear=False), patch(
            'loi_he_thong.auto_promotion.current_code_version',
            return_value='disk-new-code',
        ), patch(
            'loi_he_thong.auto_promotion.current_config_version',
            return_value='config',
        ):
            ledger = Path(temp) / 'promotion.json'
            ledger.write_text(json.dumps({
                'validation_started_at': 0.0, 'code_version': 'code',
                'config_version': 'config', 'armed': True,
            }))
            result = PromotionController(ledger).evaluate(state, now=1.0)
        self.assertEqual(result['code_version'], 'code')
        self.assertFalse(result['armed'])
        self.assertEqual(result['opportunity_events'], 0)
        self.assertEqual(result['shadow_trades'], 0)
        self.assertIn(
            'RUNTIME_VERSION_DRIFT_RESTART_REQUIRED', result['blockers']
        )
        self.assertEqual(
            result['validation_restart_reason'],
            'RUNTIME_VERSION_DRIFT_RESTART_REQUIRED',
        )

    def test_signed_pnl_deltas_preserve_post_baseline_losses(self):
        totals = {
            'opportunities': 110, 'qualified': 80, 'captured': 60,
            'guardian_samples': 200, 'trades': 35,
            'gross_profit': 2.0, 'gross_loss': 1.5,
            'realized': 0.7, 'stress': -0.1,
        }
        persisted = {
            'baseline_opportunities': 100, 'baseline_qualified': 70,
            'baseline_captured': 50, 'baseline_guardian_samples': 100,
            'baseline_trades': 30, 'baseline_gross_profit': 1.0,
            'baseline_gross_loss': 1.0, 'baseline_realized': 1.0,
            'baseline_stress': 0.2,
        }
        delta = PromotionController._deltas(totals, persisted)
        self.assertAlmostEqual(delta['realized'], -0.3)
        self.assertAlmostEqual(delta['stress'], -0.3)
        self.assertEqual(delta['trades'], 5.0)


if __name__ == '__main__':
    unittest.main()
