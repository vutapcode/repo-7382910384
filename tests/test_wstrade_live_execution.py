import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from loi_he_thong import authority_contracts
from loi_he_thong import execution_causal_revalidation
from loi_he_thong import ignition_core, ignition_signals
from loi_he_thong import market_thesis


path = Path(__file__).parents[1] / '3_thuc_thi' / 'wstrade_live_execution.py'
spec = spec_from_file_location('test_wstrade_live_execution_module', path)
live = module_from_spec(spec)
spec.loader.exec_module(live)


class FakeApi:
    def __init__(self, stop_ok=True, partial=False, cancel_algo_status=200):
        self.stop_ok = stop_ok
        self.partial = partial
        self.cancel_algo_status = cancel_algo_status
        self.orders = []
        self.positions = []
        self.algos = []
    async def new_order(self, symbol, side, kind, quantity=None, **kwargs):
        self.orders.append((side, kind, quantity, kwargs))
        if self.partial and len(self.orders) == 1:
            self.positions = [{
                'positionSide': kwargs.get('positionSide'),
                'positionAmt': '0.0004',
            }]
            return {'status': 'PARTIALLY_FILLED', 'avgPrice': '78000', 'executedQty': '0.0004', 'orderId': 1}, 200
        position_side = str(kwargs.get('positionSide') or '')
        opens = (
            (position_side == 'LONG' and side == 'BUY')
            or (position_side == 'SHORT' and side == 'SELL')
        )
        if kind == 'MARKET' and opens:
            self.positions = [{
                'positionSide': position_side,
                'positionAmt': str(quantity or 0.001),
            }]
        elif kind == 'MARKET' and position_side:
            self.positions = []
        return {'status': 'FILLED', 'avgPrice': '78000', 'orderId': len(self.orders)}, 200
    async def new_algo_order(self, **kwargs):
        if not self.stop_ok:
            return {'code': -1}, 400
        row = {'algoId': 7, 'clientAlgoId': kwargs['clientAlgoId']}
        self.algos.append(row)
        return row, 200
    async def get_open_algo_orders(self, symbol=None): return self.algos, 200
    async def get_open_orders(self, symbol=None): return [], 200
    async def get_positions(self, symbol=None): return self.positions, 200
    async def cancel_algo_order(self, algo_id): return {}, self.cancel_algo_status
    async def cancel_all_open_orders(self, symbol): return {}, 200
    async def cancel_all_algo_orders(self, symbol): self.algos.clear(); return {}, 200


class MakerApi(FakeApi):
    def __init__(self, terminal='CANCELED', executed='0.0004'):
        super().__init__()
        self.terminal = terminal
        self.executed = executed
        self.canceled = False

    async def new_order(self, symbol, side, kind, quantity=None, **kwargs):
        self.orders.append((side, kind, quantity, kwargs))
        if kind == 'LIMIT':
            return {'status': 'NEW', 'executedQty': '0', 'orderId': 1}, 200
        return {'status': 'FILLED', 'avgPrice': '78000', 'orderId': 2}, 200

    async def query_order(self, symbol, client_id):
        status = self.terminal if self.canceled else 'NEW'
        return {
            'status': status, 'executedQty': self.executed if self.canceled else '0',
            'orderId': 1,
        }, 200

    async def cancel_order(self, symbol, order_id):
        self.canceled = True
        return {'status': self.terminal, 'orderId': order_id}, 200


class TimeoutMarketApi(FakeApi):
    async def new_order(self, symbol, side, kind, quantity=None, **kwargs):
        self.orders.append((side, kind, quantity, kwargs))
        return {'status': 'NEW', 'executedQty': '0', 'orderId': 1}, 599

    async def query_order(self, symbol, client_id):
        return {'status': 'NEW', 'executedQty': '0', 'orderId': 1}, 200


class AckOnlyStopApi(FakeApi):
    async def new_algo_order(self, **kwargs):
        return {'algoId': 77, 'clientAlgoId': kwargs['clientAlgoId']}, 200


class StopTimeoutApi(FakeApi):
    async def new_algo_order(self, **kwargs):
        return {'code': 'NETWORK', 'message': 'timeout'}, 599


class UnsafeControlApi(FakeApi):
    def control_plane_snapshot(self, **kwargs):
        return {
            'version': 'EXECUTION_CONTROL_PLANE_V1',
            'health': 'UNSAFE_FOR_NEW_ENTRY',
            'reason': 'MEASURED_P95_EXCEEDS_OPPORTUNITY_BUDGET',
            'entry_allowed': False,
            'sample_count': 4,
        }


def state():
    return SimpleNamespace(
        run_id='run', wstrade_live_armed=True, mainnet_shadow_position=None,
        execution_best_bid=99.9, execution_best_ask=100.1,
        exchange_filters={'tick_size': 0.1}, atr_1m=1.0,
        mainnet_worst_roundtrip_fee_bps=18.0, account_hedge_mode=True,
        execution_unknown=False, trading_enabled=True,
        wstrade_user_stream_ready=True,
    )


def action_approved_result(result, side='LONG'):
    result = dict(result or {})
    episode_id = str(result.get('causal_episode_id') or 'episode-test')
    result.update({
        'decision': 'GO', 'reason': 'IGNITION_PROVED', 'side': side,
        'causal_episode_id': episode_id,
    })
    truth = market_thesis.build(result)
    action = authority_contracts.seal(
        'ACTION', 'TEST_ACTION', episode_id,
        {'action': 'ACT_TAKER_NOW'},
    )
    execution = execution_causal_revalidation.pending_contract(episode_id)
    safety = authority_contracts.seal(
        'SAFETY', 'TEST_SAFETY', episode_id,
        {'safety_state': 'SAFE', 'safety_action': 'ALLOW'},
    )
    result['authority_contracts'] = authority_contracts.bundle(
        truth, action, execution, safety,
    )
    result['entry_thesis_handoff'] = authority_contracts.freeze_entry_handoff(
        result['authority_contracts'], expected_side=side,
        expected_episode_id=episode_id,
    )
    return result


class LiveExecutionTests(unittest.TestCase):
    def test_pre_submit_revalidation_rejects_stale_or_changed_causal_state(self):
        s = state()
        s.bias_state = "LONG"
        s.bias_confidence = 0.8
        s.bias_updated_at = 10.0
        s.execution_price_time = 10.0
        s._ignition_signal_engine = ignition_signals.SignalEngine()
        for venue in s._ignition_signal_engine.venues.values():
            venue.epoch = 1
            venue.clock_valid = True
        s.canonical_reserved_context = {
            "opportunity_id": 7,
            "causal_episode_id": "episode-7",
            "epochs": {"binance_spot": 1, "futures": 1},
        }
        result = {
            "ts": 9.5,
            "canonical_opportunity_id": 7,
            "causal_episode_id": "episode-7",
            "ignition": {
                "cash_venues": ["binance_spot"],
                "proof_type": "METAORDER_CONTINUATION",
                "proof_venue": "binance_spot",
                "bias_snapshot": {
                    "direction": "LONG", "confidence": 0.8,
                    "updated_at": 9.0,
                },
                "current_cash_conversion": {
                    "confirmed": True,
                    "venues": {"binance_spot": {
                        "receive_time_ms": 9_800, "epoch": 1,
                        "imbalance": 0.8,
                        "price_conversion_bps": 0.3,
                    }},
                },
                "clock_quality": {
                    "binance_spot": {"epoch": 1},
                    "futures": {"epoch": 1},
                },
            },
        }
        basis, dependencies, proof_hash = ignition_core._freeze_authority_proof(
            result["ignition"], "LONG", "METAORDER_CONTINUATION",
            "episode-7",
        )
        result.update({
            "authority_basis": basis,
            "authority_dependencies": dependencies,
            "authority_proof_hash": proof_hash,
        })
        s.canonical_reserved_context.update({
            "authority_basis": basis,
            "authority_dependencies": dependencies,
            "authority_proof_hash": proof_hash,
        })
        ok, reason = live._revalidate_before_submit(
            s, "LONG", result, now=10.0,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "PASS")
        ok, reason = live._revalidate_before_submit(
            s, "LONG", dict(result, ts=8.0), now=10.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "CAUSAL_PROOF_STALE")
        s.bias_state = "SHORT"
        ok, reason = live._revalidate_before_submit(
            s, "LONG", result, now=10.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "BIAS_SIDE_CHANGED")

    def test_post_rest_spread_widen_blocks_before_live_submit(self):
        async def run():
            api, s = FakeApi(), state()
            s.mainnet_commission_verified = True
            s.mainnet_commission_source = "BINANCE_ACCOUNT_COMMISSION_RATE"
            s.mainnet_maker_fee_bps = 2.0
            s.mainnet_taker_fee_bps = 5.0
            result = action_approved_result({
                "execution_policy": "TAKER", "phase": "RELEASE",
                "canonical_opportunity_id": 7,
                "causal_episode_id": "episode-7",
            })
            result["execution_cost_contract"] = (
                live.verified_cost_model.freeze_execution_cost_contract(result, s)
            )
            s.execution_best_bid = 99.5
            s.execution_best_ask = 100.5
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live, '_revalidate_before_submit', return_value=(True, 'PASS')
            ):
                pos = await live.open_position(api, s, 'LONG', result, now=1.0)
            self.assertIsNone(pos)
            self.assertEqual(api.orders, [])
            self.assertEqual(
                s.wstrade_live_last_cost_revalidation['reason'],
                'EXECUTION_COST_WORSE_THAN_DECISION',
            )
        asyncio.run(run())

    def test_entry_thesis_preserves_cash_authority_for_guardian(self):
        thesis = live._entry_causal_thesis({
            'causal': {
                'evidence_groups': {
                    'cash_price': ['spot', 'coinbase'],
                    'cash_flow': ['spot'],
                },
                'handoff': {'status': 'SPOT_HANDOFF'},
                'oi_intent': {'intent': 'POSITION_BUILD'},
            },
        })
        self.assertEqual(thesis['primary_cash_anchor'], 'spot')
        self.assertEqual(thesis['cash_anchors'], ['coinbase', 'spot'])
        self.assertEqual(thesis['handoff_status'], 'SPOT_HANDOFF')

    def test_ignition_thesis_preserves_frozen_long_context(self):
        thesis = live._entry_causal_thesis({'ignition': {
            'cash_venues': ['binance_spot'], 'proposer': 'binance_spot',
            'bias_snapshot': {
                'direction': 'LONG', 'confidence': 0.8,
                'direction_context': {
                    'context_side': 'LONG', 'phase': 'ESTABLISHED_TREND',
                    'candidate_side': 'ABSTAIN', 'hysteresis': 'STABLE',
                    'price_vote': 'LONG',
                    'flow_vote': 'LONG', 'oi_regime': 'NEW_LONG_BUILD',
                },
            },
        }})
        self.assertEqual(thesis['bias_thesis']['context_side'], 'LONG')
        self.assertEqual(thesis['bias_thesis']['phase'], 'ESTABLISHED_TREND')
        self.assertEqual(thesis['bias_thesis']['flow_vote'], 'LONG')
        self.assertEqual(thesis['bias_thesis']['direction'], 'LONG')
        self.assertEqual(thesis['bias_thesis']['hysteresis'], 'STABLE')
        self.assertEqual(
            thesis['market_thesis']['version'],
            'MARKET_THESIS_V3_AUTHORITY_SEPARATED',
        )
        self.assertEqual(thesis['market_thesis']['side'], 'LONG')
        self.assertTrue(thesis['market_thesis']['pnl_independent'])
        self.assertNotIn('best_r', thesis['market_thesis'])
        self.assertNotIn('entry_price', thesis['market_thesis'])

    def test_promotion_requires_private_user_stream(self):
        async def run():
            s = state()
            s.wstrade_user_stream_ready = False
            promoted = await live.promote(object(), s)
            self.assertFalse(promoted)
            self.assertEqual(
                s.wstrade_live_arm_reason, 'PRIVATE_USER_STREAM_NOT_READY'
            )
        asyncio.run(run())

    def test_live_exchange_filters_are_parsed_for_fixed_lot_preflight(self):
        parsed = live._btc_filters({'symbols': [{
            'symbol': 'BTCUSDT', 'filters': [
                {'filterType': 'MARKET_LOT_SIZE', 'minQty': '0.001', 'maxQty': '100', 'stepSize': '0.001'},
                {'filterType': 'PRICE_FILTER', 'tickSize': '0.1'},
                {'filterType': 'MIN_NOTIONAL', 'notional': '50'},
            ],
        }]})
        self.assertEqual(parsed['min_qty'], 0.001)
        self.assertEqual(parsed['step_size'], 0.001)
        self.assertEqual(parsed['min_notional'], 50.0)

    def test_5_4_wallet_has_positive_startup_headroom_near_78k(self):
        s = state()
        s.mainnet_worst_roundtrip_fee_bps = 18.0
        with patch.dict('os.environ', {
            'WSTRADE_MAX_PLANNED_LOSS_USDT': '0.60',
            'WSTRADE_MARGIN_RESERVE_USDT': '0.50',
            'WSTRADE_QTY_BTC': '0.001',
            'WSTRADE_LEVERAGE': '20',
        }, clear=False):
            stop, plan = live._risk_geometry(s, 'LONG', 78_000.0)
        required = 78_000.0 * 0.001 / 20.0 + plan[
            'planned_worst_loss_usdt'
        ] + 0.50
        self.assertTrue(plan['eligible'], plan)
        self.assertLess(stop, 78_000.0)
        self.assertLess(required, 5.4)
        self.assertGreater(5.4 - required, 0.30)

    def test_hard_stop_tick_rounding_never_shrinks_minimum_distance(self):
        s = state()
        s.exchange_filters = {'tick_size': 0.1}
        s.mainnet_worst_roundtrip_fee_bps = 18.0
        with patch.object(live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60):
            long_stop, long_plan = live._risk_geometry(s, 'LONG', 77_124.8)
            short_stop, short_plan = live._risk_geometry(s, 'SHORT', 77_124.8)
        self.assertTrue(long_plan['eligible'], long_plan)
        self.assertTrue(short_plan['eligible'], short_plan)
        self.assertGreaterEqual(long_plan['stop_pct'], 0.0035)
        self.assertGreaterEqual(short_plan['stop_pct'], 0.0035)
        self.assertLess(long_stop, 77_124.8)
        self.assertGreater(short_stop, 77_124.8)

    def test_fill_requires_verified_exchange_stop(self):
        async def run():
            api, s = FakeApi(), state()
            with patch.object(live.mainnet_safety, 'exchange_entry_gate', new=AsyncMock(return_value=(True, 'PASS', {}))), patch.object(live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60):
                pos = await live.open_position(api, s, 'LONG', {
                    'execution_policy': 'TAKER', 'phase': 'RELEASE'
                }, now=1.0)
            self.assertIsNotNone(pos)
            self.assertEqual(pos.hard_sl_algo_id, 7)
            self.assertTrue(pos.active)
            self.assertEqual(pos.execution_cost_plan['execution_style'], 'TAKER')
        asyncio.run(run())

    def test_stop_ack_without_exchange_query_proof_is_not_protected(self):
        async def run():
            api, s = AckOnlyStopApi(), state()
            events = []
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60
            ):
                pos = await live.open_position(
                    api, s, 'LONG', {
                        'execution_policy': 'TAKER', 'phase': 'RELEASE'
                    }, now=1.0,
                    event_callback=lambda event, payload: events.append(
                        (event, payload)
                    ),
                )
            self.assertIsNone(pos)
            self.assertEqual([row[1] for row in api.orders], ['MARKET', 'MARKET'])
            self.assertEqual(s.mainnet_shadow_position_status, 'FLAT')
            states = [
                payload['state'] for event, payload in events
                if event == 'LIVE_EXECUTION_TRANSACTION'
            ]
            self.assertIn('PROTECTION_ACKNOWLEDGED', states)
            self.assertIn('PROTECTION_VERIFICATION_FAILED', states)
            self.assertNotIn('POSITION_PROTECTED', states)
        asyncio.run(run())

    def test_stop_submit_timeout_and_unknown_query_flattens_fail_closed(self):
        async def run():
            api, s = StopTimeoutApi(), state()
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60
            ):
                pos = await live.open_position(
                    api, s, 'SHORT', {
                        'execution_policy': 'TAKER', 'phase': 'RELEASE'
                    }, now=1.0,
                )
            self.assertIsNone(pos)
            self.assertEqual(s.mainnet_shadow_position_status, 'FLAT')
            self.assertEqual(
                s.wstrade_live_last_stop_failure['post_status'], 599
            )
            self.assertFalse(s.wstrade_live_armed)
        asyncio.run(run())

    def test_unknown_emergency_flatten_is_not_submitted_twice(self):
        class Api(FakeApi):
            async def new_order(
                self, symbol, side, kind, quantity=None, **kwargs
            ):
                if len(self.orders) == 0:
                    return await super().new_order(
                        symbol, side, kind, quantity, **kwargs
                    )
                self.orders.append((side, kind, quantity, kwargs))
                return {
                    'status': 'NEW', 'executedQty': '0', 'orderId': 2
                }, 599

            async def query_order(self, symbol, client_id):
                return {
                    'status': 'NEW', 'executedQty': '0', 'orderId': 2
                }, 200

        async def run():
            api, s = Api(stop_ok=False), state()
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60
            ):
                pos = await live.open_position(
                    api, s, 'LONG', {
                        'execution_policy': 'TAKER', 'phase': 'RELEASE'
                    }, now=1.0,
                )
            self.assertIsNone(pos)
            self.assertTrue(s.wstrade_execution_recovery_required)
            self.assertEqual(len(api.orders), 2)
            result = await live.reconcile(api, s, now=2.0)
            self.assertEqual(result, 'HARD_STOP_MISSING_UNRESOLVED')
            self.assertEqual(len(api.orders), 2)
        asyncio.run(run())

    def test_execution_transaction_is_checkpointed_before_stop_submit(self):
        async def run():
            api, s = FakeApi(), state()
            checkpoints = []
            s.wstrade_runtime_state_save = lambda: checkpoints.append({
                'transaction_state': (
                    getattr(s, 'wstrade_execution_transaction', {}) or {}
                ).get('state'),
                'position_status': getattr(
                    s, 'mainnet_shadow_position_status', None
                ),
                'unprotected': bool(
                    getattr(s, 'wstrade_unprotected_exposure', False)
                ),
            })
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60
            ):
                pos = await live.open_position(
                    api, s, 'LONG', {
                        'execution_policy': 'TAKER', 'phase': 'RELEASE'
                    }, now=1.0,
                )
            self.assertIsNotNone(pos)
            self.assertIn({
                'transaction_state': 'UNPROTECTED_EXPOSURE',
                'position_status': 'UNPROTECTED_EXPOSURE',
                'unprotected': True,
            }, checkpoints)
            self.assertTrue(any(
                row['transaction_state'] == 'PROTECTION_SENT'
                and row['unprotected']
                for row in checkpoints
            ))
            transaction = s.wstrade_execution_transaction
            states = [row['state'] for row in transaction['transitions']]
            self.assertLess(states.index('FILL_CONFIRMED'), states.index(
                'UNPROTECTED_EXPOSURE'
            ))
            self.assertLess(states.index('UNPROTECTED_EXPOSURE'), states.index(
                'PROTECTION_SENT'
            ))
            self.assertLess(states.index('PROTECTION_ACKNOWLEDGED'), states.index(
                'PROTECTION_VERIFIED'
            ))
            self.assertEqual(transaction['state'], 'POSITION_PROTECTED')
        asyncio.run(run())

    def test_unsafe_control_plane_blocks_new_entry_but_sends_no_order(self):
        async def run():
            api, s = UnsafeControlApi(), state()
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ):
                pos = await live.open_position(
                    api, s, 'LONG', {
                        'execution_policy': 'TAKER', 'phase': 'RELEASE'
                    }, now=1.0,
                )
            self.assertIsNone(pos)
            self.assertEqual(api.orders, [])
            self.assertEqual(
                s.wstrade_live_last_entry_gate['reason'],
                'EXECUTION_CONTROL_PLANE_UNSAFE_FOR_NEW_ENTRY',
            )
        asyncio.run(run())

    def test_stop_failure_immediately_flattens_and_never_publishes_position(self):
        async def run():
            api, s = FakeApi(stop_ok=False), state()
            with patch.object(live.mainnet_safety, 'exchange_entry_gate', new=AsyncMock(return_value=(True, 'PASS', {}))), patch.object(live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60):
                pos = await live.open_position(api, s, 'SHORT', {
                    'execution_policy': 'TAKER', 'phase': 'RELEASE'
                }, now=1.0)
            self.assertIsNone(pos)
            self.assertIsNone(s.mainnet_shadow_position)
            self.assertEqual([row[1] for row in api.orders], ['MARKET', 'MARKET'])
            self.assertTrue(s.wstrade_live_last_entry_outcome['capture_required'])
            self.assertEqual(
                s.wstrade_live_last_entry_outcome['reason'],
                'HARD_STOP_PLACEMENT_FAILED',
            )
        asyncio.run(run())

    def test_post_fill_risk_failure_immediately_flattens_before_stop(self):
        async def run():
            api, s = FakeApi(), state()
            events = []
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.0001
            ):
                pos = await live.open_position(api, s, 'LONG', {
                    'execution_policy': 'TAKER',
                    'phase': 'RELEASE',
                }, now=1.0, event_callback=lambda event, payload: events.append(
                    (event, payload)
                ))
            self.assertIsNone(pos)
            self.assertEqual([row[1] for row in api.orders], ['MARKET', 'MARKET'])
            self.assertEqual(api.algos, [])
            self.assertFalse(
                s.wstrade_live_last_post_fill_rejection['risk_plan']['eligible']
            )
            self.assertTrue(s.wstrade_live_last_entry_outcome['capture_required'])
            self.assertEqual(
                [event for event, _ in events].count(
                    'ENTRY_FILLED_THEN_FLATTENED'
                ), 1,
            )
        asyncio.run(run())

    def test_partial_entry_is_flattened_and_never_adopted(self):
        async def run():
            api, s = FakeApi(partial=True), state()
            with patch.object(live.mainnet_safety, 'exchange_entry_gate', new=AsyncMock(return_value=(True, 'PASS', {}))), patch.object(live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60):
                pos = await live.open_position(api, s, 'LONG', {
                    'execution_policy': 'TAKER', 'phase': 'RELEASE'
                }, now=1.0)
            self.assertIsNone(pos)
            self.assertEqual(api.orders[-1][2], 0.0004)
            self.assertEqual(s.wstrade_live_last_partial_fill['executed_qty'], 0.0004)
            self.assertTrue(s.wstrade_live_last_entry_outcome['capture_required'])
        asyncio.run(run())

    def test_partial_fill_unknown_flatten_holds_until_reconcile(self):
        class Api(FakeApi):
            async def new_order(self, symbol, side, kind, quantity=None, **kwargs):
                self.orders.append((side, kind, quantity, kwargs))
                if len(self.orders) == 1:
                    return {
                        'status': 'PARTIALLY_FILLED', 'avgPrice': '78000',
                        'executedQty': '0.0004', 'orderId': 1,
                    }, 200
                return {'status': 'NEW', 'executedQty': '0'}, 200

        async def run():
            api, s = Api(), state()
            events = []
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60
            ):
                pos = await live.open_position(
                    api, s, 'LONG', {
                        'execution_policy': 'TAKER', 'phase': 'RELEASE',
                    }, now=1.0,
                    event_callback=lambda event, payload: events.append((event, payload)),
                )
            self.assertIsNone(pos)
            self.assertTrue(s.wstrade_execution_recovery_required)
            self.assertFalse(s.wstrade_live_last_entry_outcome['capture_required'])
            self.assertIn(
                'ENTRY_FILL_RECOVERY_PENDING',
                [event for event, _ in events],
            )

            result = await live.reconcile(
                api, s, now=2.0,
                event_callback=lambda event, payload: events.append((event, payload)),
            )
            self.assertEqual(result, 'RECOVERY_VERIFIED_FLAT_AFTER_FILL')
            self.assertTrue(s.wstrade_live_last_entry_outcome['capture_required'])
            self.assertEqual(
                [event for event, _ in events].count(
                    'ENTRY_FILLED_THEN_FLATTENED'
                ), 1,
            )
        asyncio.run(run())

    def test_http_200_emergency_order_must_still_be_filled(self):
        class Api(FakeApi):
            async def new_order(self, symbol, side, kind, quantity=None, **kwargs):
                return {'status': 'NEW', 'executedQty': '0'}, 200

        async def run():
            api, s = Api(), state()
            _, status = await live._emergency_flatten(api, s, 'LONG', 0.001)
            self.assertEqual(status, 599)
            self.assertTrue(s.wstrade_execution_recovery_required)
            self.assertFalse(s.wstrade_live_armed)
        asyncio.run(run())

    def test_market_timeout_nonfill_seals_entry_for_reconciliation(self):
        async def run():
            api, s = TimeoutMarketApi(), state()
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60
            ):
                pos = await live.open_position(api, s, 'LONG', {
                    'execution_policy': 'TAKER',
                    'phase': 'RELEASE',
                }, now=1.0)
            self.assertIsNone(pos)
            self.assertTrue(s.wstrade_execution_recovery_required)
            self.assertEqual(s.execution_unknown_reason, 'MARKET_ENTRY_UNVERIFIED')
            self.assertFalse(s.wstrade_live_armed)
        asyncio.run(run())

    def test_intent_checkpoint_failure_blocks_submit_and_seals(self):
        async def run():
            api, s = FakeApi(), state()
            s.wstrade_runtime_state_save = lambda: (_ for _ in ()).throw(
                OSError('disk full')
            )
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60
            ):
                pos = await live.open_position(api, s, 'LONG', {
                    'execution_policy': 'TAKER',
                    'phase': 'RELEASE',
                }, now=1.0)
            self.assertIsNone(pos)
            self.assertEqual(api.orders, [])
            self.assertTrue(s.wstrade_execution_recovery_required)
            self.assertEqual(
                s.execution_unknown_reason, 'EXECUTION_INTENT_CHECKPOINT_FAILED'
            )
        asyncio.run(run())

    def test_close_with_unverified_stop_cancel_enters_recovery(self):
        async def run():
            api, s = FakeApi(cancel_algo_status=599), state()
            with patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60
            ):
                pos = await live.open_position(api, s, 'LONG', {
                    'execution_policy': 'TAKER',
                    'phase': 'RELEASE',
                }, now=1.0)
                closed = await live.close_position(api, s, pos, 'TEST', now=2.0)
            self.assertTrue(closed)
            self.assertFalse(pos.active)
            self.assertTrue(s.wstrade_execution_recovery_required)
            self.assertEqual(s.execution_unknown_reason, 'ORPHAN_STOP_CANCEL_UNVERIFIED')
        asyncio.run(run())

    def test_recovery_latch_waits_for_durable_flat_checkpoint(self):
        async def run():
            api, s = FakeApi(), state()
            s.mainnet_shadow_position = SimpleNamespace(active=False, live=True)
            s.wstrade_execution_recovery_required = True
            s.execution_unknown = True
            s.shadow_persistence_dirty = True
            s.wstrade_runtime_state_save = lambda: (_ for _ in ()).throw(
                OSError('disk full')
            )
            result = await live.reconcile(api, s, now=1.0)
            self.assertEqual(result, 'RECOVERY_CHECKPOINT_FAILED')
            self.assertTrue(s.wstrade_execution_recovery_required)
            self.assertTrue(s.execution_unknown)

            saved = []
            s.wstrade_runtime_state_save = lambda: saved.append('flat')
            result = await live.reconcile(api, s, now=2.0)
            self.assertEqual(result, 'FLAT')
            self.assertEqual(saved, ['flat'])
            self.assertFalse(s.wstrade_execution_recovery_required)
            self.assertFalse(s.execution_unknown)
            self.assertFalse(s.shadow_persistence_dirty)
        asyncio.run(run())

    def test_maker_partial_is_terminal_before_emergency_flatten(self):
        async def run():
            api, s = MakerApi(), state()
            with patch.object(live, 'MAKER_TTL_SECONDS', 0.0), patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60
            ):
                pos = await live.open_position(api, s, 'LONG', {
                    'execution_policy': 'MAKER',
                    'phase': 'LOADING', 'edge_tier': {'cost_ok': True},
                }, now=1.0)
            self.assertIsNone(pos)
            self.assertTrue(api.canceled)
            self.assertEqual([row[1] for row in api.orders], ['LIMIT', 'MARKET'])
            self.assertFalse(getattr(s, 'wstrade_execution_recovery_required', False))
        asyncio.run(run())

    def test_nonterminal_maker_never_flattens_before_recovery_cancel(self):
        async def run():
            api, s = MakerApi(terminal='NEW', executed='0.0004'), state()
            with patch.object(live, 'MAKER_TTL_SECONDS', 0.0), patch.object(
                live.mainnet_safety, 'exchange_entry_gate',
                new=AsyncMock(return_value=(True, 'PASS', {})),
            ), patch.object(
                live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60
            ):
                pos = await live.open_position(api, s, 'LONG', {
                    'execution_policy': 'MAKER',
                    'phase': 'LOADING', 'edge_tier': {'cost_ok': True},
                }, now=1.0)
            self.assertIsNone(pos)
            self.assertEqual([row[1] for row in api.orders], ['LIMIT'])
            self.assertTrue(s.wstrade_execution_recovery_required)
            self.assertFalse(s.wstrade_live_armed)
        asyncio.run(run())

    def test_reconcile_flattens_immediately_when_exchange_stop_disappears(self):
        async def run():
            api, s = FakeApi(), state()
            with patch.object(live.mainnet_safety, 'exchange_entry_gate', new=AsyncMock(return_value=(True, 'PASS', {}))), patch.object(live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60):
                pos = await live.open_position(api, s, 'LONG', {
                    'execution_policy': 'TAKER', 'phase': 'RELEASE'
                }, now=1.0)
            api.positions = [{'positionSide': 'LONG', 'positionAmt': '0.001'}]
            api.algos.clear()
            result = await live.reconcile(api, s, now=2.0)
            self.assertEqual(result, 'HARD_STOP_MISSING_FLATTENED')
            self.assertFalse(pos.active)
            self.assertFalse(s.wstrade_live_armed)
        asyncio.run(run())

    def test_reconcile_recognizes_exchange_stop_exit(self):
        async def run():
            api, s = FakeApi(), state()
            with patch.object(live.mainnet_safety, 'exchange_entry_gate', new=AsyncMock(return_value=(True, 'PASS', {}))), patch.object(live.mainnet_safety, 'max_planned_loss_usdt', return_value=0.60):
                pos = await live.open_position(api, s, 'SHORT', {
                    'execution_policy': 'TAKER', 'phase': 'RELEASE'
                }, now=1.0)
            api.positions = []
            result = await live.reconcile(api, s, now=2.0)
            self.assertEqual(result, 'EXCHANGE_CLOSED_POSITION')
            self.assertFalse(pos.active)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
