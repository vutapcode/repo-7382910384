import importlib.util
import os
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile = load('aug13_profile_test', 'loi_he_thong/strategy_profile.py')
guardian = load(
    'aug13_guardian_test',
    '3_thuc_thi/ve_si_lenh/bao_ve_khan_cap.py',
)
executor = load('aug13_executor_test', '3_thuc_thi/dat_lenh.py')
radar = load('aug13_radar_test', '2_suy_luan_mapping/map_gia_tick.py')


def score(progress15, progress60, held_power, opposing_power, floor=30.0):
    return {
        'activation_floor': floor,
        'momentum_breakdown': {'horizons': {
            '15': {'price_progress_atr': progress15},
            '60': {'price_progress_atr': progress60},
        }},
        'sides': {
            'LONG': {'trade_power': held_power},
            'SHORT': {'trade_power': opposing_power},
        },
    }


class Aug13GuardianTests(unittest.TestCase):
    def test_same_source_flow_is_deduped(self):
        result = guardian.assess_aug13_causal_exit(
            'LONG', 99.0, 100.0,
            {'adverse': ['FLASH_FLOW', 'FOOTPRINT']},
            score(-0.4, -0.2, 5.0, 30.0),
            {'available': True, 'realizable_edge_lcb': -2.0},
        )
        self.assertEqual(result['adverse_families'], ['AGGTRADE_FLOW'])
        self.assertFalse(result['candidate'])

    def test_aug13_false_shark_does_not_cut_positive_momentum(self):
        result = guardian.assess_aug13_causal_exit(
            'LONG', 101.0, 100.0,
            {'adverse': ['FLASH_FLOW', 'FOOTPRINT', 'BOOK']},
            score(1.50, 2.06, 18.0, 2.0),
            {'available': True, 'realizable_edge_lcb': -1.0},
            age_seconds=352.0,
        )
        self.assertEqual(result['independent_adverse_count'], 2)
        self.assertFalse(result['price_damage'])
        self.assertFalse(result['candidate'])

    def test_independent_reversal_can_close_full_position(self):
        result = guardian.assess_aug13_causal_exit(
            'LONG', 99.0, 100.0,
            {'adverse': ['FLASH_FLOW', 'BOOK']},
            score(-0.35, -0.10, 4.0, 25.0),
            {'available': True, 'realizable_edge_lcb': -2.0},
        )
        self.assertTrue(result['price_damage'])
        self.assertTrue(result['opposing_dominant'])
        self.assertTrue(result['economics_lost'])
        self.assertTrue(result['candidate'])

    def test_confirmed_structure_reversal_uses_short_confirmation(self):
        result = guardian.assess_aug13_causal_exit(
            'LONG', 99.0, 100.0,
            {'adverse': ['BOOK']},
            score(-0.30, -0.05, 8.0, 9.0),
            {'available': False},
            structure_transition='TRANSITION_BEARISH',
            structure_break_streak=2,
        )
        self.assertTrue(result['structural_candidate'])
        self.assertEqual(result['confirmation_seconds'], 0.5)

    def test_exact_setup_invalidation_needs_price_damage_before_exit(self):
        damaged = guardian.assess_aug13_causal_exit(
            'LONG', 99.0, 100.0,
            {'adverse': ['FLASH_FLOW']},
            score(-0.30, -0.05, 8.0, 9.0),
            {'available': True, 'realizable_edge_lcb': 1.0},
            setup_terminal={'state': 'INVALIDATED', 'reason': 'zone moved'},
        )
        self.assertTrue(damaged['lifecycle_candidate'])
        self.assertEqual(damaged['confirmation_seconds'], 0.5)

        healthy = guardian.assess_aug13_causal_exit(
            'LONG', 101.0, 100.0,
            {'adverse': ['FLASH_FLOW']},
            score(0.30, 0.10, 8.0, 9.0),
            {'available': True, 'realizable_edge_lcb': 1.0},
            setup_terminal={'state': 'INVALIDATED', 'reason': 'zone moved'},
        )
        self.assertTrue(healthy['setup_invalidated'])
        self.assertFalse(healthy['lifecycle_candidate'])
        self.assertFalse(healthy['candidate'])


class Aug13LifecycleTests(unittest.TestCase):
    def test_profile_retry_contract(self):
        with mock.patch.dict(os.environ, {
            'SMC_STRATEGY_PROFILE': profile.AUG13_EARLY_HYBRID_V1,
        }):
            self.assertTrue(profile.aug13_early_hybrid_enabled())
            self.assertTrue(profile.passive_reason_is_retryable('EXPIRED_UNFILLED'))
            self.assertTrue(profile.passive_reason_is_retryable(
                'PASSIVE_TOXICITY_EXPIRED'
            ))
            self.assertTrue(profile.passive_reason_is_retryable(
                'MAINNET_RISK_BUDGET_INVALID'
            ))
            self.assertFalse(profile.passive_reason_is_retryable('THESIS_INVALIDATED'))

    def test_mainnet_risk_wait_is_not_mislabeled_as_weak_edge(self):
        evaluation = executor.dynamic_entry_gate_evaluation({
            'structural_fee_floor_pass': True,
            'realizable_edge_lcb': 27.3278,
            'entry_policy': 'PASSIVE_RETEST_ONLY',
            'mainnet_risk_plan': {
                'eligible': False,
                'reason': 'SOFT_SL_OUTSIDE_SAFE_BUDGET',
                'planned_worst_loss_usdt': 0.11997,
                'max_planned_loss_usdt': 0.12,
            },
        }, mainnet_fixed=True)
        self.assertFalse(evaluation['qualified'])
        self.assertTrue(evaluation['maker_edge_pass'])
        self.assertFalse(evaluation['risk_budget_pass'])
        self.assertTrue(evaluation['retryable'])
        self.assertEqual(evaluation['reason'], 'MAINNET_RISK_BUDGET_INVALID')

    def test_missing_mainnet_risk_plan_fails_closed_not_retry_forever(self):
        evaluation = executor.dynamic_entry_gate_evaluation({
            'structural_fee_floor_pass': True,
            'realizable_edge_lcb': 27.3278,
        }, mainnet_fixed=True)
        self.assertFalse(evaluation['qualified'])
        self.assertFalse(evaluation['retryable'])
        self.assertEqual(evaluation['reason'], 'MAINNET_RISK_PLAN_MISSING')

    def test_retry_wait_reuses_one_cycle(self):
        setup = {
            'setup_id': 'setup-1', 'generation': 1,
            'opportunity_id': 'opp-1', 'semantic_key': 'opp-1',
            'expires_mono': 0.0,
        }
        signal = {
            'setup_id': 'setup-1', 'setup_generation': 1,
            'opportunity_id': 'opp-1', 'decision_price': 100.0,
        }
        state = SimpleNamespace(
            trade_cycles={'cycle-1': {'status': 'ENTRY_SUBMITTING'}},
            opportunity_retry_cycles={}, setup_cooldowns={}, journal_events=[],
        )
        with mock.patch.object(executor.journal_mod, 'record_decision_stage'):
            self.assertTrue(executor._mark_cycle_retry_wait(
                state, signal, setup, 'cycle-1', 'EXPIRED_UNFILLED'
            ))
            reused = executor._create_or_reuse_cycle(
                state, signal, setup, 0.001, 100.0,
                {'realizable_edge_lcb': 2.0},
            )
        self.assertEqual(reused, 'cycle-1')
        self.assertEqual(len(state.trade_cycles), 1)
        self.assertEqual(state.trade_cycles['cycle-1']['status'], 'ENTRY_SUBMITTING')
        self.assertEqual(setup['passive_intent_state'], 'RETRY_WAIT')
        self.assertGreater(state.setup_cooldowns['opp-1'], time.monotonic())

    def _breakout_state(self):
        now_mono = time.monotonic()
        opportunity = {
            'opportunity_id': 'opp-breakout-1',
            'base_key': 'SHORT:63366',
            'direction': 'SHORT',
            'level': 63366.0,
            'mode': 'TRANSITION-BREAKOUT',
            'state': 'READY',
            'created_at': time.time() - 60.0,
            'created_mono': now_mono - 60.0,
            'expires_mono': now_mono + 1200.0,
            'event_ids': ['m1-break-1'],
            'entry_style': 'PASSIVE_RETEST',
            'passive_entry_price': 63366.0,
            'target': 63240.0,
            'target2': 63180.0,
            'target_basis': 'NEXT_LIQUIDITY',
            'minimum_raw_target_bps': 10.0,
        }
        state = SimpleNamespace(
            breakout_opportunities={'SHORT:63366': opportunity},
            setup_generation=0,
            structure_version=7,
            best_bid=63360.0,
            best_ask=63360.1,
            journal_events=deque(maxlen=100),
            setup_outcomes=deque(maxlen=20),
            setup_followups=deque(maxlen=20),
            continuous_shadow_registry={},
            side_calibration_shadow_registry={},
            run_id='run-test',
        )
        candidate = radar._active_opportunity_candidates(state, now_mono)[0]
        setup = radar._new_setup(state, candidate, 1, now_mono - 60.1)
        return state, opportunity, candidate, setup, now_mono

    def test_setup_ttl_rolls_without_killing_breakout_opportunity(self):
        state, opportunity, _, setup, now_mono = self._breakout_state()
        setup['continuous_eligible_since_mono'] = now_mono - 0.25
        setup['max_continuous_score'] = 72.0
        setup['last_score'] = {'trade_power': 36.5}
        setups = {'breakout': setup}
        with mock.patch.dict(os.environ, {
            'SMC_STRATEGY_PROFILE': profile.AUG13_EARLY_HYBRID_V1,
            'SMC_SCORER_VERSION': 'CONTINUOUS_V2',
        }):
            radar._invalidate(
                state, setups, 'breakout', 'TTL', 63360.0, time.time()
            )
            candidates = radar._active_opportunity_candidates(
                state, time.monotonic()
            )
            rolled = radar._new_setup(state, candidates[0], 2, time.monotonic())
        self.assertEqual(setups, {})
        self.assertEqual(setup['state'], 'ROLLED')
        self.assertEqual(opportunity['state'], 'READY')
        self.assertEqual(opportunity['setup_roll_count'], 1)
        self.assertAlmostEqual(rolled['max_continuous_score'], 72.0)
        self.assertAlmostEqual(rolled['peak_trade_power'], 36.5)
        self.assertIn('continuous_eligible_since_mono', rolled)
        self.assertEqual(len(state.setup_outcomes), 0)
        self.assertFalse(hasattr(state, 'attempted_breakout_events'))

    def test_structural_invalidation_still_terminates_opportunity(self):
        state, opportunity, _, setup, _, = self._breakout_state()
        setups = {'breakout': setup}
        with mock.patch.dict(os.environ, {
            'SMC_STRATEGY_PROFILE': profile.AUG13_EARLY_HYBRID_V1,
            'SMC_SCORER_VERSION': 'CONTINUOUS_V2',
        }):
            radar._invalidate(
                state, setups, 'breakout', 'structure changed',
                63360.0, time.time(),
            )
        self.assertEqual(opportunity['state'], 'INVALIDATED')
        self.assertEqual(opportunity['terminal_reason'], 'structure changed')
        self.assertEqual(len(state.setup_outcomes), 1)


if __name__ == '__main__':
    unittest.main()
