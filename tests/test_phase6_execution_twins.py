import unittest
from loi_he_thong import phase6_execution_twins as twins


def op():
    return {
        "side": "LONG",
        "decision_available_time": 10.0,
        "market_truth_hash": "truth",
        "causal_episode_id": "ep",
        "wal_identity": "wal",
        "candidate_population_hash": "pop",
        "causal_wave_id": "wave",
        "guardian_version": "g",
        "fill_model_version": "f",
        "frozen_cost_hash": "c",
        "quantity": 2.0,
        "maker_queue_ahead": 3.0,
        "maker_ttl_ms": 600,
        "taker_frozen_cost_bps": 2.0,
        "maker_frozen_cost_bps": 1.0,
    }


def events():
    return [
        {"type": "STATE", "available_time": 10.0, "wave_alive": True, "feed_valid": True, "gap_free": True},
        {"type": "BBO", "available_time": 10.0, "valid_until": 11.0, "bid": 99.0, "ask": 100.0},
        {"type": "STATE", "available_time": 10.1, "wave_alive": True, "feed_valid": True, "gap_free": True},
        {"type": "BBO", "available_time": 10.1, "valid_until": 11.0, "bid": 100.0, "ask": 101.0},
        {"type": "STATE", "available_time": 10.3, "wave_alive": True, "feed_valid": True, "gap_free": True},
        {"type": "BBO", "available_time": 10.3, "valid_until": 11.0, "bid": 101.0, "ask": 102.0},
        {"type": "STATE", "available_time": 10.6, "wave_alive": True, "feed_valid": True, "gap_free": True},
        {"type": "BBO", "available_time": 10.6, "valid_until": 11.0, "bid": 102.0, "ask": 103.0},
        {"type": "TRADE", "available_time": 10.0, "price": 99.0, "qty": 100.0, "aggressor": "SELL"},
        {"type": "TRADE", "available_time": 10.2, "price": 99.0, "qty": 2.0, "aggressor": "SELL"},
        {"type": "TRADE", "available_time": 10.4, "price": 99.0, "qty": 2.0, "aggressor": "SELL"},
        {"type": "EXIT", "available_time": 10.8, "price": 104.0},
    ]


def guardian(e):
    if e.get("type") == "EXIT":
        return {"exit": True, "exit_price": e["price"], "reason": "TEST_EXIT"}
    return None


class Phase6TwinsTests(unittest.TestCase):
    def test_wait_uses_only_available_events_and_taker_uses_contemporaneous_bbo(self):
        r = twins.evaluate(op(), events(), guardian_step=guardian)
        rows = {x["branch"]: x for x in r["branches"]}
        self.assertEqual(rows["TAKER_NOW"]["entry_price"], 100.0)
        self.assertEqual(rows["WAIT100"]["entry_price"], 101.0)
        self.assertEqual(rows["WAIT300"]["entry_price"], 102.0)
        self.assertEqual(rows["WAIT500"]["entry_price"], 102.0)
        self.assertEqual(rows["WAIT600"]["entry_price"], 103.0)

    def test_maker_order_after_trade_cannot_use_prior_trade(self):
        r = twins.evaluate(op(), events(), guardian_step=guardian)
        maker = {x["branch"]: x for x in r["branches"]}["MAKER_IF_EXECUTABLE"]
        self.assertNotEqual(maker["status"], "FILLED")

    def test_touch_without_queue_volume_cannot_full_fill(self):
        o = op()
        o["maker_queue_ahead"] = 10.0
        r = twins.evaluate(o, events(), guardian_step=guardian)
        maker = {x["branch"]: x for x in r["branches"]}["MAKER_IF_EXECUTABLE"]
        self.assertEqual(maker["status"], "PARTIAL_OR_UNFILLED")

    def test_frozen_cost_applied_once(self):
        r = twins.evaluate(op(), events(), guardian_step=guardian)
        taker = {x["branch"]: x for x in r["branches"]}["TAKER_NOW"]
        self.assertEqual(taker["outcome"]["cost_applications"], 1)

    def test_path_metrics_use_only_executable_bbo(self):
        r = twins.evaluate(op(), events(), guardian_step=guardian)
        outcome = {row["branch"]: row for row in r["branches"]}[
            "TAKER_NOW"
        ]["outcome"]
        self.assertGreater(outcome["mfe_bps"], 0.0)
        self.assertLessEqual(outcome["mae_bps"], 0.0)
        self.assertIsNotNone(outcome["time_to_positive_net"])
        self.assertGreaterEqual(outcome["capture_ratio"], 0.0)
        self.assertLessEqual(outcome["capture_ratio"], 1.0)

    def test_twins_share_identity_and_are_deterministic(self):
        a = twins.evaluate(op(), events(), guardian_step=guardian)
        b = twins.evaluate(op(), events(), guardian_step=guardian)
        self.assertEqual(a["identity"]["wal_identity"], "wal")
        self.assertEqual(a["identity"]["guardian_version"], "g")
        self.assertEqual(a["deterministic_hash"], b["deterministic_hash"])


if __name__ == "__main__":
    unittest.main()
