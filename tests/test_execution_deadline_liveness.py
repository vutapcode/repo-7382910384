import asyncio
import time
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).parents[1]


def _load(name, relative):
    spec = spec_from_file_location(name, ROOT / relative)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live = _load(
    "test_execution_deadline_live_module",
    "3_thuc_thi/wstrade_live_execution.py",
)
binance_api = _load(
    "test_execution_deadline_binance_module",
    "3_thuc_thi/binance_api.py",
)


class AcceptedAckDelayedApi:
    def __init__(self):
        self.order_open = False
        self.cancel_calls = []

    async def new_order(self, *args, **kwargs):
        # Exchange-side acceptance may precede the delayed HTTP response.
        self.order_open = True
        await asyncio.sleep(1.0)
        return {"status": "NEW", "orderId": 71}, 200

    async def cancel_order(self, symbol, identity):
        self.cancel_calls.append((symbol, identity))
        self.order_open = False
        return {"status": "CANCELED", "orderId": 71}, 200

    async def query_order(self, symbol, client_id):
        return {
            "status": "NEW" if self.order_open else "CANCELED",
            "executedQty": "0",
            "orderId": 71,
        }, 200


class ExecutionDeadlineLivenessTests(unittest.TestCase):
    def test_binance_transport_deadline_is_typed_and_bounded(self):
        async def run():
            api = object.__new__(binance_api.BinanceAPI)
            api.client = object()
            api._control_plane = None
            started = time.monotonic()
            result, status = await api._call(
                lambda: (time.sleep(1.0), {"ok": True})[1],
                operation="SLOW_TEST",
                deadline_monotonic=time.monotonic() + 0.03,
                timeout_cap_seconds=0.03,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(status, 599)
            self.assertEqual(result["code"], "EXECUTION_DEADLINE_EXCEEDED")
            self.assertLess(elapsed, 0.20)

        asyncio.run(run())

    def test_maker_ttl_includes_delayed_post_ack(self):
        async def run():
            api = AcceptedAckDelayedApi()
            state = SimpleNamespace(
                run_id="run",
                execution_best_bid=100.0,
                execution_best_ask=100.1,
                wstrade_user_stream_orders={},
                wstrade_live_armed=True,
            )
            started = time.monotonic()
            order, status, client_id = await live._hybrid_entry(
                api,
                state,
                "LONG",
                {"execution_policy": "MAKER"},
                0.001,
                time.time(),
                deadline_monotonic=time.monotonic() + 0.12,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(status, 409)
            self.assertEqual(order["status"], "CANCELED")
            self.assertFalse(api.order_open)
            self.assertEqual(len(api.cancel_calls), 1)
            self.assertTrue(client_id.startswith("ws_maker_"))
            self.assertLess(elapsed, 0.20)

        asyncio.run(run())

    def test_priority_single_writer_selects_exit_before_reconcile(self):
        async def run():
            state = SimpleNamespace()
            order = []
            entered = asyncio.Event()
            release = asyncio.Event()

            async def holder():
                async with live._execution_guard(state, priority=10):
                    entered.set()
                    await release.wait()

            async def waiter(label, priority):
                async with live._execution_guard(state, priority=priority):
                    order.append(label)

            active = asyncio.create_task(holder())
            await entered.wait()
            reconcile = asyncio.create_task(waiter("RECONCILE", 20))
            await asyncio.sleep(0)
            close = asyncio.create_task(waiter("EXIT", 0))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(active, reconcile, close)
            self.assertEqual(order, ["EXIT", "RECONCILE"])

        asyncio.run(run())

    def test_guardian_exit_is_next_after_inflight_reconcile(self):
        class Api:
            def __init__(self):
                self.release = asyncio.Event()
                self.first_read = asyncio.Event()
                self.log = []
                self.positions = [{
                    "positionSide": "LONG", "positionAmt": "0.001",
                }]
                self.algos = [{"algoId": 9, "clientAlgoId": "stop-9"}]

            async def _read(self, name, value):
                self.log.append(name)
                self.first_read.set()
                await self.release.wait()
                return value, 200

            async def get_positions(self, symbol=None):
                return await self._read("READ_POSITIONS", list(self.positions))

            async def get_open_algo_orders(self, symbol=None):
                return await self._read("READ_ALGOS", list(self.algos))

            async def get_open_orders(self, symbol=None):
                return await self._read("READ_ORDERS", [])

            async def new_order(self, *args, **kwargs):
                self.log.append("EXIT_SUBMIT")
                self.positions = []
                return {"status": "FILLED", "avgPrice": "100", "orderId": 2}, 200

            async def cancel_algo_order(self, algo_id):
                self.log.append("CANCEL_STOP")
                self.algos = []
                return {}, 200

        async def run():
            api = Api()
            position = SimpleNamespace(
                active=True, live=True, side="LONG", qty=0.001,
                hard_sl_algo_id=9, hard_sl_client_algo_id="stop-9",
                entry_price=100.0,
            )
            state = SimpleNamespace(
                run_id="run", wstrade_live_armed=True,
                wstrade_live_entry_allowed=False,
                wstrade_user_stream_ready=True,
                mainnet_shadow_position=position,
                wstrade_execution_recovery_required=False,
                execution_unknown=False,
                wstrade_daily_gate_checked_at=1.0,
            )
            first = asyncio.create_task(live.reconcile(api, state, now=1.0))
            await api.first_read.wait()
            second = asyncio.create_task(live.reconcile(api, state, now=1.0))
            await asyncio.sleep(0)
            close = asyncio.create_task(
                live.close_position(api, state, position, "HARD_RISK", now=1.0)
            )
            await asyncio.sleep(0)
            api.release.set()
            await asyncio.gather(first, second, close)
            # Three reads belong to the in-flight reconciliation.  The queued
            # exit must then win before a second reconciliation can issue I/O.
            self.assertEqual(api.log[3], "EXIT_SUBMIT")
            self.assertFalse(position.active)

        asyncio.run(run())

    def test_production_adapter_enables_measured_entry_latency_authority(self):
        with patch.object(binance_api, "UMFutures"):
            api = binance_api.BinanceAPI("key", "secret")
        self.assertTrue(api._control_plane._latency_authority_enabled)


if __name__ == "__main__":
    unittest.main()
