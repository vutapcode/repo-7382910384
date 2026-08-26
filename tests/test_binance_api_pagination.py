
import asyncio
import importlib.util
from pathlib import Path
import unittest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "3_thuc_thi" / "binance_api.py"
    spec = importlib.util.spec_from_file_location("binance_api_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


binance_api = _load_module()


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    def make_api(self):
        api = binance_api.BinanceAPI.__new__(binance_api.BinanceAPI)
        api.testnet = False
        return api

    async def test_all_orders_paginates_beyond_1000(self):
        api = self.make_api()
        calls = []

        class Client:
            def get_all_orders(self, **kwargs):
                return None

        api.client = Client()

        async def fake_call(func, *args, **kwargs):
            calls.append(dict(kwargs))
            start_id = kwargs.get("orderId", 1)
            if start_id == 1 and len(calls) == 1:
                start_id = 1
            remaining = 2505 - (start_id - 1)
            count = max(0, min(1000, remaining))
            return [
                {"orderId": order_id}
                for order_id in range(start_id, start_id + count)
            ], 200

        api._call = fake_call
        rows, status = await api.get_all_orders(
            "BTCUSDT", start_time=1234567890000, end_time=1234567990000
        )

        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 2505)
        self.assertEqual(rows[0]["orderId"], 1)
        self.assertEqual(rows[-1]["orderId"], 2505)
        self.assertEqual(calls[1]["orderId"], 1001)
        self.assertEqual(calls[2]["orderId"], 2001)
        self.assertNotIn("startTime", calls[1])
        self.assertEqual(calls[0]["endTime"], calls[1]["endTime"])

    async def test_income_history_paginates_beyond_1000_and_dedupes(self):
        api = self.make_api()
        calls = []

        class Client:
            def get_income_history(self, **kwargs):
                return None

        api.client = Client()

        async def fake_call(func, *args, **kwargs):
            calls.append(dict(kwargs))
            page = kwargs["page"]
            start = (page - 1) * 1000 + 1
            remaining = 2505 - (start - 1)
            count = max(0, min(1000, remaining))
            return [
                {
                    "tranId": tran_id,
                    "time": 1234567890000 + tran_id,
                    "incomeType": "REALIZED_PNL",
                    "income": "0.01",
                    "asset": "USDT",
                    "symbol": "BTCUSDT",
                }
                for tran_id in range(start, start + count)
            ], 200

        api._call = fake_call
        rows, status = await api.get_income_history(
            symbol="BTCUSDT",
            start_time=1234567890000,
            end_time=1234567990000,
        )

        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 2505)
        self.assertEqual([call["page"] for call in calls], [1, 2, 3])
        self.assertEqual(calls[0]["endTime"], calls[-1]["endTime"])

    async def test_pagination_safety_limit_fails_closed(self):
        api = self.make_api()

        class Client:
            def get_all_orders(self, **kwargs):
                return None

        api.client = Client()

        async def fake_call(func, *args, **kwargs):
            start_id = kwargs.get("orderId", 1)
            return [
                {"orderId": order_id}
                for order_id in range(start_id, start_id + 1000)
            ], 200

        api._call = fake_call
        payload, status = await api.get_all_orders(
            "BTCUSDT",
            start_time=1234567890000,
            end_time=1234567990000,
            max_pages=2,
        )

        self.assertEqual(status, 599)
        self.assertEqual(payload["code"], "PAGINATION")


if __name__ == "__main__":
    unittest.main()
