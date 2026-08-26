import unittest

from recorder.main import RecorderResearchTracker


def trade(venue, at_ms, side="LONG", sequence=1, price=100.0):
    buy = 0.03 if side == "LONG" else 0.0
    sell = 0.03 if side == "SHORT" else 0.0
    return {
        "stream": venue + "_trade_100ms",
        "receive_time_ms": at_ms + 99,
        "event_time_ms": at_ms + 50,
        "sequence_start": sequence,
        "sequence_end": sequence,
        "previous_sequence": sequence - 1 if sequence > 1 else None,
        "payload": {
            "bucket_start_ms": at_ms,
            "first_price": price,
            "last_price": price + (0.001 if side == "LONG" else -0.001),
            "high": price + 0.001,
            "low": price - 0.001,
            "buy_qty": buy,
            "sell_qty": sell,
        },
    }


class StubOutcomes:
    def observe(self, _record):
        return None


class PrecursorContinuityRecorderTests(unittest.TestCase):
    def test_borderline_candidate_gets_three_receive_time_horizons(self):
        emitted = []
        tracker = RecorderResearchTracker(
            lambda stream, payload, event_time_ms=None: emitted.append(
                (stream, payload, event_time_ms)
            ),
            StubOutcomes(),
        )
        sequence = {"binance_spot": 0, "coinbase_spot": 0}
        origin = 20_000
        for second in range(5, 20):
            for venue in sequence:
                sequence[venue] += 1
                tracker.observe(trade(
                    venue, second * 1_000, "LONG", sequence[venue],
                    100.0 + second * 0.01,
                ))
        tracker.observe({
            "stream": "bot_event", "receive_time_ms": origin + 200,
            "event_time_ms": origin + 100,
            "payload": {
                "event": "DECISION_EVALUATED", "side": "ABSTAIN",
                "ignition": {
                    "research_side": "LONG",
                    "research_receive_time_ms": origin,
                },
                "opportunity_research": {
                    "research_candidate_id": "ign-research:test",
                    "pre_bias": {"band": "BORDERLINE", "direction": "LONG"},
                    "leader": "UNKNOWN",
                    "first_blocking_gate": "BIAS_ABSTAIN",
                },
                "decision_record": {"cycle_id": "cycle-1", "inputs": {}},
            },
        })
        rows = [payload for stream, payload, _ in emitted
                if stream == "precursor_continuity"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]["horizons"]), {"3", "6", "15"})
        self.assertFalse(rows[0]["authority"])
        self.assertFalse(rows[0]["canonical_continuity_confirmed"])
        self.assertEqual(
            rows[0]["horizons"]["3"]["status"],
            "SAME_CAUSAL_WAVE_CANDIDATE",
        )

    def test_sequence_gap_prevents_continuity_label(self):
        emitted = []
        tracker = RecorderResearchTracker(
            lambda stream, payload, event_time_ms=None: emitted.append(
                (stream, payload, event_time_ms)
            ),
            StubOutcomes(),
        )
        for venue in ("binance_spot", "coinbase_spot"):
            tracker.observe(trade(venue, 8_000, "LONG", 1, 100.0))
            tracker.observe(trade(venue, 9_000, "LONG", 3, 100.01))
        report = tracker._precursor_horizon("LONG", 10_000, 3)
        self.assertEqual(report["status"], "INVALID_SEQUENCE_GAP")


if __name__ == "__main__":
    unittest.main()
