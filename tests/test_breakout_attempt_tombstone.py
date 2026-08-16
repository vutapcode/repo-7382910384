import asyncio
import importlib.util
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


radar = load_module('radar_breakout_tombstone', '2_suy_luan_mapping/map_gia_tick.py')
ram = load_module('ram_breakout_tombstone', 'loi_he_thong/bo_nho_ram.py')


class BreakoutAttemptTombstoneTests(unittest.TestCase):
    def _candidate(self, event_id='breakout:1:LONG', kind='breakout'):
        return {
            'key': 'TRANSITION-BREAKOUT:LONG',
            'zone_id': 'TRANSITION-BREAKOUT:LONG',
            'mode': 'TRANSITION-BREAKOUT',
            'bias': 'LONG',
            'zone': 100.0,
            'kind': kind,
            'breakout_event_id': event_id,
        }

    def test_breakout_event_id_propagates_to_candidate_and_setup(self):
        event = {
            'flag': True,
            'direction': 'LONG',
            'level': 100.0,
            'event_id': 'breakout:stable:LONG',
        }
        trend_candidates = radar.build_candidates({
            'modes': ['TREND-PULLBACK', 'TREND-BREAKOUT'],
            'bias': 'LONG',
            'pullback_zones': [99.0],
            'breakout_level': 100.0,
        }, event)
        breakout = next(
            item for item in trend_candidates if item['kind'] == 'breakout'
        )
        zone = next(item for item in trend_candidates if item['kind'] == 'zone')
        self.assertEqual(breakout['breakout_event_id'], event['event_id'])
        self.assertNotIn('breakout_event_id', zone)

        state = ram.SharedState()
        state.structure_version = 3
        state.best_bid, state.best_ask = 99.9, 100.0
        setup = radar._new_setup(state, breakout, 1, time.monotonic())
        self.assertEqual(setup['breakout_event_id'], event['event_id'])
        self.assertFalse(setup['claimed_once'])

        transition = radar.build_candidates(
            {'modes': ['STANDBY'], 'bias': 'NONE'}, event,
        )[0]
        self.assertEqual(transition['breakout_event_id'], event['event_id'])

    def test_claimed_fee_blocked_event_is_one_shot_but_new_event_and_zone_are_not(self):
        state = ram.SharedState()
        state.structure_version = 1
        state.best_bid, state.best_ask = 99.9, 100.0
        candidate = self._candidate()
        setup = radar._new_setup(state, candidate, 1, time.monotonic())
        setup['claimed_once'] = True
        # Executor writes this terminal state into the shared setup after a fee
        # block; Radar observes and retires the same object on its next pass.
        setup['state'] = 'INVALIDATED'
        setups = {candidate['key']: setup}

        radar._invalidate(
            state, setups, candidate['key'], 'terminal',
            reference_price=100.0, now_wall=1000.0,
        )

        self.assertNotIn(candidate['key'], setups)
        self.assertTrue(radar._breakout_attempt_blocked(state, candidate))
        tombstone = state.attempted_breakout_events['breakout:1:LONG']
        self.assertEqual(tombstone['setup_id'], setup['setup_id'])
        self.assertEqual(tombstone['terminal_state'], 'INVALIDATED')

        new_event = self._candidate('breakout:2:LONG')
        self.assertFalse(radar._breakout_attempt_blocked(state, new_event))
        replacement = radar._new_setup(
            state, new_event, 2, time.monotonic()
        )
        self.assertEqual(replacement['breakout_event_id'], 'breakout:2:LONG')

        zone = self._candidate('breakout:1:LONG', kind='zone')
        self.assertFalse(radar._breakout_attempt_blocked(state, zone))

    def test_only_claimed_failed_terminals_are_recorded_and_store_is_bounded(self):
        state = ram.SharedState()
        state.structure_version = 1
        state.best_bid, state.best_ask = 99.9, 100.0

        unclaimed = radar._new_setup(
            state, self._candidate('breakout:unclaimed'), 1, time.monotonic()
        )
        unclaimed['state'] = 'INVALIDATED'
        radar._invalidate(
            state, {'event': unclaimed}, 'event', 'terminal', now_wall=1.0
        )
        self.assertNotIn(
            'breakout:unclaimed',
            getattr(state, 'attempted_breakout_events', {}),
        )

        executed = radar._new_setup(
            state, self._candidate('breakout:executed'), 2, time.monotonic()
        )
        executed['claimed_once'] = True
        executed['state'] = 'EXECUTED'
        radar._invalidate(
            state, {'event': executed}, 'event', 'terminal', now_wall=2.0
        )
        self.assertNotIn(
            'breakout:executed',
            getattr(state, 'attempted_breakout_events', {}),
        )

        expired = radar._new_setup(
            state, self._candidate('breakout:expired'), 3, time.monotonic()
        )
        expired['claimed_once'] = True
        expired['state'] = 'EXECUTING'
        radar._invalidate(
            state, {'event': expired}, 'event', 'TTL', now_wall=3.0
        )
        self.assertEqual(
            state.attempted_breakout_events['breakout:expired'][
                'terminal_state'
            ],
            'EXPIRED',
        )

        for index in range(radar.BREAKOUT_EVENT_TOMBSTONE_LIMIT + 3):
            terminal = {
                'kind': 'breakout',
                'claimed_once': True,
                'state': 'INVALIDATED',
                'breakout_event_id': f'breakout:bounded:{index}',
                'setup_id': f'setup:{index}',
            }
            radar._remember_terminal_breakout_attempt(
                state, terminal, 'INVALIDATED', 'terminal', 10.0 + index
            )

        self.assertEqual(
            len(state.attempted_breakout_events),
            radar.BREAKOUT_EVENT_TOMBSTONE_LIMIT,
        )
        self.assertIn(
            f'breakout:bounded:{radar.BREAKOUT_EVENT_TOMBSTONE_LIMIT + 2}',
            state.attempted_breakout_events,
        )
        self.assertNotIn('breakout:expired', state.attempted_breakout_events)

    def test_radar_coalesces_same_opportunity_and_claims_new_structure(self):
        asyncio.run(self._exercise_breakout_event_lifecycle())

    async def _exercise_breakout_event_lifecycle(self):
        state = ram.SharedState()
        state.system_ready = True
        state.trading_enabled = True
        state.best_bid, state.best_ask = 99.9, 100.0
        state.atr_1m = 1.0
        state.current_mode = {'modes': ['STANDBY'], 'bias': 'NONE'}
        state.breakout_m1 = {
            'flag': True,
            'direction': 'LONG',
            'level': 100.0,
            'event_id': 'breakout:loop:1',
            'ts': time.time(),
        }
        claims = []

        def claim_once(
            shared_state, mode_info, mode, bias, setup=None,
            decision_snapshot=None,
        ):
            setup['state'] = 'EXECUTING'
            claims.append((setup['setup_id'], setup['breakout_event_id']))
            return {'setup_id': setup['setup_id']}

        original_commander = radar.chi_huy_truong.phan_tich_va_ra_lenh
        radar.chi_huy_truong.phan_tich_va_ra_lenh = claim_once
        task = asyncio.create_task(radar.vong_lap_radar(state))
        try:
            await self._wait_until(lambda: len(claims) == 1)
            claimed_setup = next(iter(state.active_setups.values()))
            self.assertTrue(claimed_setup['claimed_once'])

            # Simulate Executor's terminal fee/geometry invalidation on the
            # shared setup object. Radar must consume it without re-claiming.
            claimed_setup['state'] = 'INVALIDATED'
            await self._wait_until(lambda: not state.active_setups)
            await asyncio.sleep(0.03)
            self.assertEqual(claims, [(
                claimed_setup['setup_id'], 'breakout:loop:1',
            )])

            state.breakout_m1 = dict(
                state.breakout_m1,
                event_id='breakout:loop:2',
                ts=time.time(),
            )
            await asyncio.sleep(0.08)
            self.assertEqual(len(claims), 1)

            # A genuinely different breakout level is a new opportunity even
            # when it arrives within the same five-minute wall-clock window.
            state.best_bid, state.best_ask = 100.9, 101.0
            state.breakout_m1 = dict(
                state.breakout_m1,
                level=101.0,
                event_id='breakout:loop:3',
                ts=time.time(),
            )
            await self._wait_until(lambda: len(claims) == 2)
            self.assertEqual(claims[1][1], 'breakout:loop:3')
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            radar.chi_huy_truong.phan_tich_va_ra_lenh = original_commander

    async def _wait_until(self, predicate):
        for _ in range(50):
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail('Radar did not reach the expected state in time')


if __name__ == '__main__':
    unittest.main()
