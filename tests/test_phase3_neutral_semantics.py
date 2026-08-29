from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loi_he_thong import ignition_core, ignition_signals, microstructure_regime


class Phase3NeutralSemanticsTests(unittest.TestCase):
    def test_raw_flow_bucket_has_complete_evidence_provenance(self):
        venue = ignition_signals._Venue("binance_spot")
        venue.push(1_001, 990, 100.0, 0.02, True)
        row = venue.finalize()
        self.assertTrue(row["evidence_id"].startswith("ev:"))
        self.assertEqual(row["root_evidence_id"], row["evidence_id"])
        self.assertEqual(row["parent_evidence_ids"], [])

    def test_derived_features_from_one_venue_share_one_wave_root(self):
        rows = [
            {
                "venue": "binance_spot", "epoch": 1,
                "bucket_start_ms": bucket,
                "evidence_id": f"raw-{bucket}",
            }
            for bucket in (1_000, 1_100, 1_200)
        ]
        provenance = ignition_core._evidence_provenance(
            "episode-1", rows, "METAORDER_CONTINUATION"
        )
        venue_nodes = [
            node for node in provenance["nodes"]
            if node["evidence_role"] == "CASH_VENUE_WAVE"
        ]
        self.assertEqual(len(venue_nodes), 1)
        self.assertEqual(len(venue_nodes[0]["parent_evidence_ids"]), 3)
        for key in ("evidence_id", "parent_evidence_ids", "root_evidence_id"):
            self.assertIn(key, venue_nodes[0])

    def test_oi_price_contraction_is_not_called_liquidation_without_force_order(self):
        state = SimpleNamespace(
            best_bid=100.99,
            best_ask=101.01,
            open_interest=800.0,
            current_vol_3s=1.0,
            current_cvd_buy_3s=1.0,
            current_cvd_sell_3s=0.0,
            vol_pct90=1.0,
            atr_1m=0.1,
            bias_state="LONG",
            _micro_regime_hist=deque([(0.0, 100.0, 1_000.0, 1.0)], maxlen=64),
        )
        with patch.object(microstructure_regime.time, "time", return_value=10.0), patch.object(
            microstructure_regime.flow_lead_engine,
            "analyze",
            return_value={"status": "WARMUP"},
        ):
            report = microstructure_regime.classify(state, "LONG")
        self.assertEqual(report["regime"], "OI_CONTRACTION_EXPANSION")
        self.assertEqual(report["oi_signature"], "OI_CONTRACTION_ACCEL")
        self.assertEqual(
            report["mechanism_hypothesis"],
            "LIQUIDATION_CANDIDATE_REQUIRES_FORCE_ORDER",
        )
        self.assertFalse(report["mechanism_confirmed"])


if __name__ == "__main__":
    unittest.main()
