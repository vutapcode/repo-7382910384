from types import SimpleNamespace
import unittest

from loi_he_thong import private_user_stream as stream


class PrivateUserStreamTests(unittest.TestCase):
    def test_order_update_is_normalized_for_execution_recovery(self):
        state = SimpleNamespace()
        result = stream.apply_event(state, {
            "e": "ORDER_TRADE_UPDATE", "E": 1_000, "T": 999,
            "o": {
                "s": "BTCUSDT", "c": "ws_entry_1", "i": 7,
                "S": "BUY", "ps": "LONG", "o": "MARKET",
                "x": "TRADE", "X": "FILLED", "q": "0.001",
                "z": "0.001", "ap": "78000", "l": "0.001",
                "L": "78000", "rp": "0",
            },
        }, received_at=1.1)
        self.assertEqual(result, "ORDER_TRADE_UPDATE")
        row = stream.order_snapshot(state, "ws_entry_1")
        self.assertEqual(row["status"], "FILLED")
        self.assertEqual(row["executedQty"], "0.001")
        self.assertEqual(row["avgPrice"], "78000")

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


if __name__ == "__main__":
    unittest.main()
