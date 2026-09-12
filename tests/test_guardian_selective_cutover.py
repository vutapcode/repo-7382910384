import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mainnet_tier_s_shadow_launcher as launcher
from loi_he_thong import market_thesis, shadow_risk_guard


CASES = (
    ("23:18 SHORT", "SHORT", 77606.3573, 77878.0, 77300.0, False),
    ("23:25 LONG", "LONG", 77490.721865, 77219.5, 77958.0, False),
    ("23:27 LONG", "LONG", 77656.24669, 77384.4, 77958.0, False),
    ("23:32 SHORT", "SHORT", 77492.474385, 77763.7, 77763.8, True),
    ("23:51 LONG", "LONG", 77911.685, 77638.9, 77638.8, True),
)


def _truth(side, episode):
    return market_thesis.build({
        "decision": "GO", "reason": "IGNITION_PROVED", "side": side,
        "causal_episode_id": episode,
        "ignition": {
            "side": side, "causal_episode_id": episode,
            "proof_type": "PERSISTENT_METAORDER",
            "proposer": "binance_spot",
            "cash_venues": ["binance_spot", "coinbase_spot"],
            "current_cash_conversion": {
                "confirmed": True,
                "accepted_cash_venues": [
                    "binance_spot", "coinbase_spot",
                ],
            },
            "clock_quality": {
                "binance_spot": {"source_health": "FRESH", "epoch": 1},
                "coinbase_spot": {"source_health": "FRESH", "epoch": 1},
            },
        },
    })


def _local_counterflow(side, episode):
    return {
        "version": "GUARDIAN_CANONICAL_OBSERVATION_V2",
        "causal_episode_id": episode,
        "position_side": side,
        "source_health": {
            "spot": "FRESH", "coinbase": "FRESH", "futures": "FRESH",
        },
        "price_horizons": {
            "3.0": {
                "threshold_bps": 1.5,
                "moves": {
                    "spot": -3.0, "coinbase": -3.0, "futures": -3.0,
                },
            },
        },
        "flow_signed_imbalances": {
            "spot": -0.7, "coinbase": -0.7, "futures": -0.7,
        },
        "oi": {"status": "NEUTRAL", "fresh": True},
        "gap_or_epoch_invalid": False,
        "observed_at_ms": 1_000,
    }


def _risk_position(side, entry, hard_sl):
    distance = abs(entry - hard_sl)
    return SimpleNamespace(
        side=side, entry_price=entry, hard_sl=hard_sl, r=distance,
        best=entry, best_r=0.0, floor_r=None, floor=None,
        stage="INITIAL", tier_mode="PROTECT", fee_r=0.1,
    )


class SelectiveGuardianCutoverTests(unittest.TestCase):
    def test_five_trade_regression_separates_pullback_from_hard_risk(self):
        for index, (name, side, entry, hard_sl, later_price, hard_hit) in enumerate(
            CASES, start=1,
        ):
            with self.subTest(case=name):
                episode = "historical-five-%d" % index
                observed = market_thesis.observe(
                    _truth(side, episode),
                    _local_counterflow(side, episode),
                )
                self.assertEqual(observed["status"], "DIVERGENCE")
                self.assertFalse(observed["old_thesis_falsified"])

                risk = shadow_risk_guard.assess(
                    _risk_position(side, entry, hard_sl), later_price,
                    guardian={"decision": "DETERIORATING", "votes": {}},
                )
                self.assertEqual(risk["decision"] == "EXIT", hard_hit)
                if hard_hit:
                    self.assertEqual(risk["reason"], "HARD_SL")

    def test_post_entry_loop_keeps_cash_wave_evidence_current_and_recorded(self):
        state = SimpleNamespace(_cross_cash_causal_wave_events=[])
        position = SimpleNamespace(position_cycle_id="position-open")
        snapshot = {
            "version": "CROSS_CASH_CAUSAL_WAVE_V1",
            "observed_at_ms": 1_000,
        }

        def observe(target, histories, now_ms):
            self.assertEqual(histories, {"spot": ()})
            self.assertEqual(now_ms, 1_000)
            target.cross_cash_causal_wave_shadow = snapshot
            target._cross_cash_causal_wave_events = [(
                "CAUSAL_WAVE_OPENED", {"causal_wave_id": "wave-1"},
            )]
            return snapshot

        with patch.object(
            launcher.ignition_signals, "snapshot", return_value={"spot": ()},
        ), patch.object(
            launcher.cross_cash_causal_wave, "observe", side_effect=observe,
        ), patch.object(launcher, "_append_event") as append:
            result = launcher._refresh_post_entry_market_evidence(
                state, position, 1.0,
            )

        self.assertEqual(result, snapshot)
        self.assertEqual(state._cross_cash_causal_wave_events, [])
        append.assert_called_once()
        self.assertEqual(append.call_args.args[0], "CAUSAL_WAVE_OPENED")
        self.assertEqual(
            append.call_args.args[1]["cycle_id"], "position-open",
        )


if __name__ == "__main__":
    unittest.main()
