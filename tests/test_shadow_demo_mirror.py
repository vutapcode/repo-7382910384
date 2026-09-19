import json
from pathlib import Path
import tempfile
import unittest

from ops.shadow_demo_mirror import DemoMirror


class FakeTestnet:
    def __init__(self, *, hedge=True, initial=0.0, fill_limit=False):
        self.hedge = hedge
        self.qty = float(initial)
        self.fill_limit = fill_limit
        self.orders = []

    def get_position_mode(self):
        return {"dualSidePosition": self.hedge}

    def get_orders(self, **_kwargs):
        return [
            {**row, "clientOrderId": row["newClientOrderId"]}
            for row in self.orders if row["status"] == "NEW"
        ]

    def exchange_info(self):
        return {"symbols": [{"symbol": "BTCUSDT", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"},
        ]}]}

    def get_position_risk(self, **_kwargs):
        if self.hedge:
            return [
                {"positionSide": "LONG", "positionAmt": str(max(0, self.qty))},
                {"positionSide": "SHORT", "positionAmt": str(min(0, self.qty))},
            ]
        return [{"positionSide": "BOTH", "positionAmt": str(self.qty)}]

    def new_order(self, **kwargs):
        order = dict(kwargs)
        order.update(orderId=len(self.orders) + 1, status="NEW", executedQty="0")
        if kwargs["type"] == "MARKET" or self.fill_limit:
            order.update(status="FILLED", executedQty=str(kwargs["quantity"]))
            signed = float(kwargs["quantity"]) * (
                1 if kwargs["side"] == "BUY" else -1
            )
            self.qty += signed
        self.orders.append(order)
        return dict(order)

    def query_order(self, *, orderId=None, origClientOrderId=None, **_kwargs):
        for row in self.orders:
            if row["orderId"] == orderId or row["newClientOrderId"] == origClientOrderId:
                return dict(row)
        raise LookupError("ORDER_NOT_FOUND")

    def cancel_order(self, *, orderId, **_kwargs):
        self.orders[orderId - 1]["status"] = "CANCELED"
        return dict(self.orders[orderId - 1])


class ShadowDemoMirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "events.jsonl"
        self.source.touch()
        self.state = Path(self.temp.name) / "mirror.json"

    def mirror(self, client):
        return DemoMirror(client, source=self.source, state_path=self.state)

    @staticmethod
    def event(event, suffix, **extra):
        return {
            "event": event,
            "event_id": "bot:run:%s" % suffix,
            "cycle_id": "shadow:LONG:1",
            "side": "LONG",
            **extra,
        }

    def append(self, row):
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def test_market_entry_and_shadow_hard_sl_exit_have_no_exchange_sl(self):
        client = FakeTestnet()
        mirror = self.mirror(client)
        self.append(self.event(
            "ENTRY", "1", actual_qty_btc=0.001,
            shadow_execution={"style": "MARKET"},
        ))
        self.append(self.event(
            "EXIT", "2", risk_reason="HARD_SL", hard_sl=90000,
        ))
        self.assertEqual(mirror.run_once(), 2)
        self.assertEqual(client.qty, 0)
        self.assertEqual([x["type"] for x in client.orders], ["MARKET", "MARKET"])
        self.assertEqual(client.orders[1]["positionSide"], "LONG")
        self.assertTrue(all(
            "closePosition" not in x and "stopPrice" not in x
            for x in client.orders
        ))
        self.assertIsNone(mirror.state["open"])

    def test_shadow_maker_fill_tries_limit_then_market_catchup(self):
        client = FakeTestnet(fill_limit=False)
        mirror = self.mirror(client)
        self.append(self.event(
            "ENTRY", "3", actual_qty_btc=0.001, price=100000.0,
            shadow_execution={"style": "MAKER_TRADE_THROUGH"},
        ))
        self.assertEqual(mirror.run_once(), 1)
        self.assertEqual(client.orders[0]["type"], "LIMIT")
        self.assertEqual(client.orders[0]["status"], "CANCELED")
        self.assertEqual(client.orders[1]["type"], "MARKET")
        self.assertEqual(client.qty, 0.001)

    def test_preexisting_demo_position_is_not_adopted_or_closed(self):
        client = FakeTestnet(initial=0.001)
        with self.assertRaisesRegex(RuntimeError, "DEMO_ACCOUNT_NOT_FLAT"):
            self.mirror(client)
        self.assertEqual(client.qty, 0.001)
        self.assertEqual(client.orders, [])

    def test_preexisting_demo_order_is_not_adopted(self):
        client = FakeTestnet()
        client.new_order(
            symbol="BTCUSDT", newClientOrderId="manual-order",
            type="LIMIT", side="BUY", quantity=0.001,
        )
        with self.assertRaisesRegex(RuntimeError, "DEMO_UNOWNED_OPEN_ORDER"):
            self.mirror(client)

    def test_first_start_does_not_replay_old_shadow_trades(self):
        self.append(self.event("ENTRY", "old", actual_qty_btc=0.001))
        client = FakeTestnet()
        mirror = self.mirror(client)
        self.assertEqual(mirror.run_once(), 0)
        self.assertEqual(client.orders, [])

    def test_restart_recovers_accepted_entry_without_duplicate_order(self):
        client = FakeTestnet()
        mirror = self.mirror(client)
        row = self.event(
            "ENTRY", "recover", actual_qty_btc=0.001,
            shadow_execution={"style": "MARKET"},
        )
        self.append(row)
        mirror.state["pending"] = {
            "action": "ENTRY", "event_id": row["event_id"],
            "cycle_id": row["cycle_id"], "side": "LONG", "qty": 0.001,
        }
        from ops.shadow_demo_mirror import _atomic_state
        _atomic_state(self.state, mirror.state)
        client.new_order(
            symbol="BTCUSDT", newClientOrderId="already-accepted",
            type="MARKET", side="BUY", quantity=0.001,
        )
        restarted = self.mirror(client)
        self.assertEqual(restarted.run_once(), 1)
        self.assertEqual(len(client.orders), 1)
        self.assertEqual(restarted.state["open"]["qty"], 0.001)

    def test_restart_recovers_completed_exit_without_duplicate_order(self):
        client = FakeTestnet(initial=0.001)
        self.source.touch()
        from ops.shadow_demo_mirror import _atomic_state
        stat = self.source.stat()
        row = self.event("EXIT", "exit-recover")
        self.append(row)
        _atomic_state(self.state, {
            "version": 1, "device": stat.st_dev, "inode": stat.st_ino,
            "offset": 0,
            "open": {"cycle_id": row["cycle_id"], "side": "LONG", "qty": 0.001},
            "pending": {
                "action": "EXIT", "event_id": row["event_id"],
                "cycle_id": row["cycle_id"], "side": "LONG", "qty": 0.001,
            },
        })
        client.qty = 0.0
        mirror = self.mirror(client)
        self.assertEqual(mirror.run_once(), 1)
        self.assertEqual(client.orders, [])
        self.assertIsNone(mirror.state["open"])


if __name__ == "__main__":
    unittest.main()
