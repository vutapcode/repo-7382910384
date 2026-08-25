import unittest
from types import SimpleNamespace

from loi_he_thong import execution_causal_revalidation as recheck
from loi_he_thong import ignition_signals


def material(venue, bucket, side, acceleration=1.0):
    sign = 1.0 if side == "LONG" else -1.0
    return {
        "venue": venue,
        "epoch": 1,
        "bucket_start_ms": bucket,
        "receive_time_ms": bucket + 100,
        "clock_valid": True,
        "side": side,
        "strong": True,
        "total_qty": ignition_signals.MIN_QTY[venue] * 2.0,
        "imbalance": sign * 0.8,
        "price_conversion_bps": sign * 0.30,
        "flow_acceleration": acceleration,
    }


def fixture():
    engine = ignition_signals.SignalEngine()
    for venue in engine.venues.values():
        venue.epoch = 1
        venue.clock_valid = True
    state = SimpleNamespace(
        _ignition_signal_engine=engine,
        canonical_reserved_context={
            "opportunity_id": 9,
            "causal_episode_id": "episode-9",
            "epochs": {"binance_spot": 1, "futures": 1},
        },
        bias_state="LONG",
        bias_confidence=0.8,
        bias_updated_at=10.0,
        execution_best_bid=99.9,
        execution_best_ask=100.1,
        execution_price_time=10.0,
        mainnet_maker_fee_bps=2.0,
        mainnet_taker_fee_bps=5.0,
        mainnet_commission_verified=True,
    )
    result = {
        "ts": 9.5,
        "canonical_opportunity_id": 9,
        "causal_episode_id": "episode-9",
        "ignition": {"cash_venues": ["binance_spot"]},
    }
    return state, result


class ExecutionCausalRevalidationTests(unittest.TestCase):
    def test_isolated_opposing_cash_bucket_cannot_veto(self):
        state, result = fixture()
        state._ignition_signal_engine.venues["binance_spot"].history.append(
            material("binance_spot", 9500, "SHORT")
        )
        ok, reason, _ = recheck.validate_submit(state, "LONG", result, 10.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "PASS")

    def test_two_adjacent_opposing_cash_buckets_veto(self):
        state, result = fixture()
        history = state._ignition_signal_engine.venues["binance_spot"].history
        history.extend([
            material("binance_spot", 9500, "SHORT"),
            material("binance_spot", 9600, "SHORT"),
        ])
        ok, reason, _ = recheck.validate_submit(state, "LONG", result, 10.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "POST_PROOF_OPPOSING_FLOW_2_BUCKETS")

    def test_maker_release_requires_cash_persistence_and_futures_response(self):
        state, result = fixture()
        cash = state._ignition_signal_engine.venues["binance_spot"].history
        futures = state._ignition_signal_engine.venues["futures"].history
        cash.extend([
            material("binance_spot", 9500, "LONG"),
            material("binance_spot", 9600, "LONG"),
        ])
        ok, reason, _ = recheck.maker_ttl_release(state, "LONG", result, 10.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "CURRENT_RELEASE_NOT_PROVED")
        futures.append(material("futures", 9600, "LONG"))
        ok, reason, detail = recheck.maker_ttl_release(
            state, "LONG", result, 10.0
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "CURRENT_RELEASE_PASS")
        self.assertEqual(detail["cash_venue"], "binance_spot")

    def test_epoch_change_fails_closed(self):
        state, result = fixture()
        state._ignition_signal_engine.venues["futures"].epoch = 2
        ok, reason, _ = recheck.validate_submit(state, "LONG", result, 10.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "EXECUTED_FLOW_EPOCH_RESET")


if __name__ == "__main__":
    unittest.main()
