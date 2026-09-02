from types import SimpleNamespace
import unittest

from loi_he_thong import private_user_stream as stream


class PrivateUserStreamTests(unittest.TestCase):
    def test_order_update_is_normalized_for_execution_recovery(self):
        state = SimpleNamespace()
        result = stream.apply_event(state, {
            "e": "ORDER_TRADE_UPDATE", "E": 1_000, "T": 999,
            "o": {
                "s": "BTCUSDT", "c": "ws_entry_1", "i": 7, "t": 100,
                "S": "BUY", "ps": "LONG", "o": "MARKET",
                "x": "TRADE", "X": "FILLED", "q": "0.001",
                "z": "0.001", "ap": "78000", "l": "0.001",
                "L": "78000", "rp": "0", "n": "0.04", "N": "USDT",
            },
        }, received_at=1.1)
        self.assertEqual(result, "ORDER_TRADE_UPDATE")
        row = stream.order_snapshot(state, "ws_entry_1")
        self.assertEqual(row["status"], "FILLED")
        self.assertEqual(row["executedQty"], "0.001")
        self.assertEqual(row["avgPrice"], "78000")
        self.assertEqual(row["commissionAmount"], "0.04")
        self.assertEqual(row["commissionAsset"], "USDT")
        self.assertEqual(row["commissionByAssetCumulative"], {"USDT": 0.04})
        self.assertEqual(row["received_at"], 1.1)
        self.assertAlmostEqual(
            state.wstrade_user_stream_last_transport_lag_ms, 100.0
        )

        second = {
            "e": "ORDER_TRADE_UPDATE", "E": 1_001, "T": 1_000,
            "o": {
                "s": "BTCUSDT", "c": "ws_entry_1", "i": 7, "t": 101,
                "S": "BUY", "ps": "LONG", "o": "MARKET",
                "x": "TRADE", "X": "FILLED", "q": "0.001",
                "z": "0.001", "ap": "78000", "l": "0.0005",
                "L": "78000", "rp": "0", "n": "0.02", "N": "USDT",
            },
        }
        stream.apply_event(state, second, received_at=1.2)
        row = stream.order_snapshot(state, "ws_entry_1")
        self.assertEqual(row["commissionByAssetCumulative"], {"USDT": 0.06})
        stream.apply_event(state, second, received_at=1.3)
        row = stream.order_snapshot(state, "ws_entry_1")
        self.assertEqual(row["commissionByAssetCumulative"], {"USDT": 0.06})

    def test_disconnect_seals_entries_but_preserves_live_authority(self):
        state = SimpleNamespace(
            wstrade_live_armed=True,
            wstrade_live_entry_allowed=True,
        )
        stream.mark_disconnected(state, "TEST", now=1.0)
        self.assertTrue(state.wstrade_live_armed)
        self.assertFalse(state.wstrade_live_entry_allowed)
        self.assertFalse(state.wstrade_user_stream_ready)

    def test_reconnect_restores_entry_only_without_recovery_latch(self):
        state = SimpleNamespace(
            wstrade_live_armed=True,
            wstrade_live_entry_allowed=False,
            wstrade_execution_recovery_required=False,
        )
        stream.mark_connected(state, now=2.0)
        self.assertTrue(state.wstrade_live_entry_allowed)
        state.wstrade_execution_recovery_required = True
        state.wstrade_live_entry_allowed = False
        stream.mark_connected(state, now=3.0)
        self.assertFalse(state.wstrade_live_entry_allowed)

        state.wstrade_execution_recovery_required = False
        state.wstrade_live_demote_pending = True
        stream.mark_connected(state, now=4.0)
        self.assertFalse(state.wstrade_live_entry_allowed)

    def test_reconnect_cannot_override_unsafe_control_plane(self):
        state = SimpleNamespace(
            wstrade_live_armed=True,
            wstrade_live_entry_allowed=False,
            wstrade_execution_recovery_required=False,
            wstrade_execution_control_plane={
                "health": "UNSAFE_FOR_NEW_ENTRY",
                "entry_allowed": False,
            },
        )
        stream.mark_connected(state, now=5.0)
        self.assertFalse(state.wstrade_live_entry_allowed)


if __name__ == "__main__":
    unittest.main()
