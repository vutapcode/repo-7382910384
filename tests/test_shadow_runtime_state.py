import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from loi_he_thong import shadow_runtime_state as runtime_state


class ShadowRuntimeStateTests(unittest.TestCase):
    def test_entry_economics_rows_are_version_bound(self):
        row = {
            "economic_contract_version": "ENTRY_ECONOMICS_V4",
            "valid": True, "side": "LONG",
            "net_pnl_bps_after_frozen_cost": 3.0,
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            'os.environ', {'SMC_JOURNAL_DIR': temp}
        ):
            source_state = SimpleNamespace(
                code_version="code-v2", strategy_config_version="config-v2",
                mainnet_shadow_position=None,
                _entry_economics_v2_rows=[row],
            )
            runtime_state.save(SimpleNamespace(
                app=SimpleNamespace(state=source_state), QTY_BTC=0.001
            ))
            same = SimpleNamespace(
                code_version="code-v2", strategy_config_version="config-v2"
            )
            runtime_state.restore(SimpleNamespace(
                app=SimpleNamespace(state=same), QTY_BTC=0.001
            ))
            changed = SimpleNamespace(
                code_version="code-v3", strategy_config_version="config-v2"
            )
            runtime_state.restore(SimpleNamespace(
                app=SimpleNamespace(state=changed), QTY_BTC=0.001
            ))
        self.assertEqual(same._entry_economics_v2_rows, [row])
        self.assertEqual(changed._entry_economics_v2_rows, [])

    def test_flat_strategy_counters_survive_restart(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            'os.environ', {'SMC_JOURNAL_DIR': temp}
        ):
            source_state = SimpleNamespace(
                code_version="code-v1",
                strategy_config_version="config-v1",
                mainnet_shadow_position=None,
                canonical_opportunity_count=17,
                canonical_opportunity_qualified=8,
                canonical_opportunity_captured=6,
                canonical_last_consumed_opportunity_id=15,
                canonical_last_captured_opportunity_id=6,
                canonical_opportunity_active=True,
                canonical_opportunity_signature=("LONG", "NORMAL", "RELEASE"),
                canonical_opportunity_active_qualified=True,
                _edge_cal_v2_rows=[("LONG", "NORMAL", "TREND", "HIGH_EDGE", 2.5)],
                mainnet_shadow_decision_evaluations=91,
                mainnet_shadow_near_misses=12,
                mainnet_shadow_funnel_counts={
                    'COUNCIL': 70, 'EDGE_OR_QUORUM': 15, 'READY': 6,
                },
            )
            runtime_state.save(SimpleNamespace(
                app=SimpleNamespace(state=source_state), QTY_BTC=0.001
            ))
            target_state = SimpleNamespace(
                code_version="code-v1",
                strategy_config_version="config-v1",
            )
            restored = runtime_state.restore(SimpleNamespace(
                app=SimpleNamespace(state=target_state), QTY_BTC=0.001
            ))

        self.assertTrue(restored)
        self.assertEqual(target_state.canonical_opportunity_count, 17)
        self.assertEqual(target_state.canonical_opportunity_qualified, 8)
        self.assertEqual(target_state.canonical_opportunity_captured, 6)
        self.assertEqual(target_state.canonical_last_consumed_opportunity_id, 15)
        self.assertFalse(target_state.canonical_opportunity_active)
        self.assertIsNone(target_state.canonical_opportunity_signature)
        self.assertEqual(
            target_state._edge_cal_v2_rows,
            [("LONG", "NORMAL", "TREND", "HIGH_EDGE", 2.5)],
        )
        self.assertEqual(target_state.mainnet_shadow_decision_evaluations, 91)
        self.assertEqual(target_state.mainnet_shadow_near_misses, 12)
        self.assertEqual(target_state.mainnet_shadow_funnel_counts['READY'], 6)

    def test_calibration_rows_do_not_cross_code_or_config_versions(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            'os.environ', {'SMC_JOURNAL_DIR': temp}
        ):
            source_state = SimpleNamespace(
                code_version="old-code",
                strategy_config_version="config-v1",
                mainnet_shadow_position=None,
                _edge_cal_v2_rows=[("LONG", "NORMAL", "TREND", "HIGH_EDGE", 2.5)],
            )
            runtime_state.save(SimpleNamespace(
                app=SimpleNamespace(state=source_state), QTY_BTC=0.001
            ))
            target_state = SimpleNamespace(
                code_version="new-code",
                strategy_config_version="config-v1",
            )
            restored = runtime_state.restore(SimpleNamespace(
                app=SimpleNamespace(state=target_state), QTY_BTC=0.001
            ))

        self.assertTrue(restored)
        self.assertEqual(target_state._edge_cal_v2_rows, [])
        self.assertEqual(target_state.edge_cal_v2_excluded_version_mismatch, 1)
        self.assertEqual(
            target_state.edge_cal_v2_last_exclusion["reason"],
            "CODE_OR_CONFIG_VERSION_MISMATCH",
        )

    def test_current_eight_field_calibration_rows_survive_same_version_restart(self):
        row = (
            "SHORT", "IGNITION", "NORMAL", "BOOTSTRAP_UNVERIFIED",
            "FAILED_REVERSION", "FUTURES", "MAKER", -3.5,
        )
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            'os.environ', {'SMC_JOURNAL_DIR': temp}
        ):
            source_state = SimpleNamespace(
                code_version="code-v2", strategy_config_version="config-v2",
                mainnet_shadow_position=None, _edge_cal_v2_rows=[row],
            )
            runtime_state.save(SimpleNamespace(
                app=SimpleNamespace(state=source_state), QTY_BTC=0.001
            ))
            target_state = SimpleNamespace(
                code_version="code-v2", strategy_config_version="config-v2",
            )
            restored = runtime_state.restore(SimpleNamespace(
                app=SimpleNamespace(state=target_state), QTY_BTC=0.001
            ))
        self.assertTrue(restored)
        self.assertEqual(target_state._edge_cal_v2_rows, [row])

    def test_nine_field_cost_rows_survive_same_version_restart(self):
        row = (
            "LONG", "IGNITION", "NORMAL", "BOOTSTRAP_UNVERIFIED",
            "METAORDER_CONTINUATION", "BINANCE_SPOT", "TAKER", 4.25, 12.5,
        )
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            'os.environ', {'SMC_JOURNAL_DIR': temp}
        ):
            source_state = SimpleNamespace(
                code_version="code-v3", strategy_config_version="config-v3",
                mainnet_shadow_position=None, _edge_cal_v2_rows=[row],
            )
            runtime_state.save(SimpleNamespace(
                app=SimpleNamespace(state=source_state), QTY_BTC=0.001
            ))
            target_state = SimpleNamespace(
                code_version="code-v3", strategy_config_version="config-v3",
            )
            restored = runtime_state.restore(SimpleNamespace(
                app=SimpleNamespace(state=target_state), QTY_BTC=0.001
            ))
        self.assertTrue(restored)
        self.assertEqual(target_state._edge_cal_v2_rows, [row])

    def test_live_position_and_guardian_counter_restore_in_recovery_mode(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            'os.environ', {'SMC_JOURNAL_DIR': temp}
        ):
            position = SimpleNamespace(
                active=True, live=True, side='LONG', qty=0.001,
                initial_qty=0.001, entry_price=100.0,
                execution_entry_price=100.0, opened_at=1.0,
                position_cycle_id='live:test', r=0.5, hard_sl=99.5,
                best=100.0, best_r=0.0, floor_r=None, floor=None,
                stage='INITIAL', tier_mode='PROTECT', fee_r=0.1,
                whale_seen=False, whale_exhaustion_since=0.0,
                whale_exhaustion_pressure=0.0, risk_px_samples=[],
                exhaustion_meta={}, guardian_s_signature=(),
                guardian_s_candidate_since=0.0, entry_client_order_id='entry-1',
                hard_sl_algo_id=7, hard_sl_client_algo_id='stop-1',
                mainnet_risk_plan={'eligible': True}, entry_lane='CORE',
                entry_causal_thesis={
                    'version': 'ENTRY_CAUSAL_THESIS_V1',
                    'primary_cash_anchor': 'spot',
                    'cash_anchors': ['spot', 'coinbase'],
                },
            )
            source_state = SimpleNamespace(
                mainnet_shadow_position=position,
                guardian_latency_samples_total=321,
            )
            runtime_state.save(SimpleNamespace(
                app=SimpleNamespace(state=source_state), QTY_BTC=0.001
            ))

            target_state = SimpleNamespace()
            restored = runtime_state.restore(SimpleNamespace(
                app=SimpleNamespace(state=target_state), QTY_BTC=0.001
            ))

        self.assertTrue(restored)
        self.assertTrue(target_state.mainnet_shadow_position.live)
        self.assertEqual(target_state.mainnet_shadow_position.hard_sl_algo_id, 7)
        self.assertEqual(
            target_state.mainnet_shadow_position.entry_causal_thesis[
                'primary_cash_anchor'
            ],
            'spot',
        )
        self.assertEqual(target_state.guardian_latency_samples_total, 321)
        self.assertIs(
            target_state.wstrade_live_position,
            target_state.mainnet_shadow_position,
        )
        self.assertTrue(target_state.wstrade_execution_recovery_required)
        self.assertTrue(target_state.execution_unknown)
        self.assertFalse(target_state.wstrade_live_entry_allowed)


if __name__ == '__main__':
    unittest.main()
