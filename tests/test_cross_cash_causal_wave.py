import unittest
from types import SimpleNamespace

from loi_he_thong import cross_cash_causal_wave as wave


def row(venue, at, price, *, side="NEUTRAL", epoch=1, qty=0.0,
        imbalance=0.0, first_price=None, health="FRESH"):
    signed = 0.0
    if side == "LONG":
        signed = price * qty
    elif side == "SHORT":
        signed = -price * qty
    return {
        "venue": venue,
        "available_time_ms": at,
        "receive_time_ms": at,
        "bucket_start_ms": at - 100,
        "epoch": epoch,
        "price": price,
        "first_price": price if first_price is None else first_price,
        "side": side,
        "total_qty": qty,
        "imbalance": imbalance,
        "signed_quote": signed,
        "clock_valid": True,
        "source_health": health,
        "evidence_id": f"{venue}:{epoch}:{at}",
    }


def converting(venue, start, *, side="LONG", epoch=1, base=100.0):
    sign = 1.0 if side == "LONG" else -1.0
    qty = 0.020 if venue == "binance_spot" else 0.004
    return [
        row(venue, start, base, side=side, epoch=epoch,
            qty=qty, imbalance=0.70 * sign),
        row(venue, start + 100, base + sign * 0.01, side=side, epoch=epoch,
            qty=qty, imbalance=0.65 * sign),
        row(venue, start + 200, base + sign * 0.02, epoch=epoch),
    ]


class CrossCashCausalWaveTests(unittest.TestCase):
    def test_flow_must_precede_and_price_response_must_hold(self):
        result = wave._venue_observation(
            converting("binance_spot", 1_000), 1_300,
        )
        self.assertEqual(result["state"], "FLOW_LED_CONVERSION")
        self.assertTrue(result["conversion_held"])
        self.assertGreater(result["response_available_ms"], result["flow_onset_available_ms"])
        self.assertGreater(result["hold_available_ms"], result["response_available_ms"])

    def test_price_before_flow_is_chase_not_conversion(self):
        venue = "binance_spot"
        rows = [
            row(venue, 900, 100.0),
            row(venue, 1_000, 100.02, first_price=100.02, side="LONG",
                qty=0.020, imbalance=0.7),
            row(venue, 1_100, 100.0205, side="LONG",
                qty=0.020, imbalance=0.7),
            row(venue, 1_200, 100.0205),
        ]
        result = wave._venue_observation(rows, 1_300)
        self.assertEqual(result["state"], "PRICE_LED_CHASE")

    def test_material_flow_without_price_response_is_nonconversion(self):
        venue = "coinbase_spot"
        rows = [
            row(venue, 1_000, 100.0, side="LONG", qty=0.004, imbalance=0.7),
            row(venue, 1_100, 99.999, side="LONG", qty=0.004, imbalance=0.7),
            row(venue, 1_200, 99.998),
        ]
        result = wave._venue_observation(rows, 1_300)
        self.assertEqual(result["state"], "FLOW_NONCONVERSION")

    def test_second_cash_can_join_after_600ms_when_conversion_survives(self):
        state = SimpleNamespace()
        histories = {
            "binance_spot": converting("binance_spot", 1_000),
            "coinbase_spot": converting("coinbase_spot", 1_900),
        }
        result = wave.observe(state, histories, 2_200)
        self.assertEqual(result["state"], "CONTROL_PERSISTING")
        self.assertEqual(result["side"], "LONG")
        roots = result["active_wave"]["cash_roots"]
        separation = abs(
            roots["coinbase_spot"]["flow_onset_available_ms"]
            - roots["binance_spot"]["flow_onset_available_ms"]
        )
        self.assertGreater(separation, 600)
        self.assertFalse(result["active_wave"]["time_is_identity_proof"])

    def test_opposite_dual_cash_control_falsifies_old_wave(self):
        state = SimpleNamespace()
        long_histories = {
            "binance_spot": converting("binance_spot", 1_000),
            "coinbase_spot": converting("coinbase_spot", 1_100),
        }
        first = wave.observe(state, long_histories, 1_400)
        old_id = first["causal_wave_id"]
        short_histories = {
            "binance_spot": converting(
                "binance_spot", 1_500, side="SHORT", base=100.0,
            ),
            "coinbase_spot": converting(
                "coinbase_spot", 1_550, side="SHORT", base=100.0,
            ),
        }
        second = wave.observe(state, short_histories, 1_850)
        names = [name for name, _ in state._cross_cash_causal_wave_events]
        self.assertEqual(names, ["CAUSAL_WAVE_TERMINATED", "CAUSAL_WAVE_OPENED"])
        transition = state._cross_cash_causal_wave_events[0][1][
            "state_transition"
        ]
        self.assertEqual(transition["state_before"], "CONTROL_PERSISTING")
        self.assertEqual(transition["state_after"], "FALSIFIED")
        self.assertNotEqual(second["causal_wave_id"], old_id)
        self.assertEqual(second["side"], "SHORT")

    def test_epoch_break_terminates_and_never_stitches(self):
        state = SimpleNamespace()
        initial = {
            "binance_spot": converting("binance_spot", 1_000, epoch=1),
            "coinbase_spot": converting("coinbase_spot", 1_100, epoch=1),
        }
        old_id = wave.observe(state, initial, 1_400)["causal_wave_id"]
        changed = {
            "binance_spot": converting("binance_spot", 1_500, epoch=2),
            "coinbase_spot": converting("coinbase_spot", 1_600, epoch=1),
        }
        new = wave.observe(state, changed, 1_900)
        self.assertNotEqual(new["causal_wave_id"], old_id)
        self.assertEqual(
            state._cross_cash_causal_wave_events[0][1]["reason"],
            "VENUE_EPOCH_BREAK",
        )

    def test_unknown_does_not_terminate_active_wave(self):
        state = SimpleNamespace()
        histories = {
            "binance_spot": converting("binance_spot", 1_000),
            "coinbase_spot": converting("coinbase_spot", 1_100),
        }
        old_id = wave.observe(state, histories, 1_400)["causal_wave_id"]
        later = wave.observe(state, histories, 8_000)
        self.assertEqual(later["causal_wave_id"], old_id)
        self.assertEqual(state._cross_cash_causal_wave_events, [])


if __name__ == "__main__":
    unittest.main()
