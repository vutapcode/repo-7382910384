import asyncio
import unittest

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from loi_he_thong import execution_control_plane


path = Path(__file__).parents[1] / "3_thuc_thi" / "binance_api.py"
spec = spec_from_file_location("test_binance_api_control_plane_module", path)
module = module_from_spec(spec)
spec.loader.exec_module(module)


class BinanceApiControlPlaneTests(unittest.TestCase):
    def api(self):
        api = object.__new__(module.BinanceAPI)
        api._control_plane = execution_control_plane.Monitor()
        return api

    def test_control_call_records_success(self):
        async def run():
            api = self.api()
            result, status = await api._call(
                lambda: {"ok": True}, operation="QUERY_ORDER", control=True
            )
            self.assertEqual(status, 200)
            self.assertEqual(result, {"ok": True})
            snapshot = api.control_plane_snapshot()
            self.assertEqual(snapshot["sample_count"], 1)
            self.assertEqual(snapshot["latest_operation"], "QUERY_ORDER")
            self.assertTrue(snapshot["entry_allowed"])
        asyncio.run(run())

    def test_control_call_records_transport_failure(self):
        def fail():
            raise TimeoutError("timeout")

        async def run():
            api = self.api()
            result, status = await api._call(
                fail, operation="NEW_ORDER", control=True
            )
            self.assertEqual(status, 599)
            self.assertEqual(result["code"], "NETWORK")
            snapshot = api.control_plane_snapshot()
            self.assertEqual(snapshot["latest_operation"], "NEW_ORDER")
            self.assertEqual(snapshot["health"], "UNSAFE_FOR_NEW_ENTRY")
            self.assertFalse(snapshot["entry_allowed"])
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
