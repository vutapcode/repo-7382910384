import unittest
from types import SimpleNamespace

from loi_he_thong import entry_s2_snapshot_quorum_hook as hook


class EntryS2SnapshotQuorumHookTests(unittest.TestCase):
    def make_runtime(self, allowed=True):
        def authorize(result, state):
            return allowed, {
                "edge_class": "HIGH_EDGE" if allowed else "LOW_EDGE",
                "cost_ok": allowed,
                "entry_mode": result.get("entry_mode", "NORMAL"),
            }

        base = SimpleNamespace()
        runtime = SimpleNamespace(edge=SimpleNamespace(authorize=authorize), base=base)
        hook.install(runtime)
        return runtime

    def test_reuses_entry_s2_snapshot_without_live_rescan(self):
        runtime = self.make_runtime(True)
        state = SimpleNamespace()
        result = {
            "entry_mode": "NORMAL",
            "ts": 1000.0,
            "s_votes": {
                "S2_multi_venue_executed_flow": {
                    "status": "PASS",
                    "metrics": {
                        "ts": 1000.0,
                        "volume_floor_btc": 0.02,
                        "supporters": ["spot", "futures"],
                        "strong_supporters": ["spot"],
                        "venues": {
                            "spot": {"volume_btc": 0.12},
                            "futures": {"volume_btc": 0.30},
                        },
                    },
                }
            },
        }

        self.assertTrue(runtime._entry_quorum_ok(result, state, 1000.01))
        self.assertEqual(state.entry_tier_s_volume_quality["source"], "ENTRY_S2_SNAPSHOT")
        self.assertEqual(state.entry_tier_s_volume_quality["supporters"], ["spot", "futures"])
        self.assertAlmostEqual(state.entry_tier_s_volume_quality["venues"]["futures"], 0.30)

    def test_original_edge_veto_is_preserved(self):
        runtime = self.make_runtime(False)
        state = SimpleNamespace()
        self.assertFalse(runtime._entry_quorum_ok({"entry_mode": "NORMAL"}, state, 1000.0))
        self.assertEqual(state.entry_edge_class, "LOW_EDGE")

    def test_dust_or_wrong_volume_schema_fails_closed(self):
        runtime = self.make_runtime(True)
        state = SimpleNamespace()
        result = {
            "entry_mode": "NORMAL",
            "ts": 1000.0,
            "s_votes": {
                "S2_multi_venue_executed_flow": {
                    "status": "PASS",
                    "metrics": {
                        "ts": 1000.0,
                        "volume_floor_btc": 0.02,
                        "supporters": ["spot", "futures"],
                        "venues": {
                            "spot": {"volume": 0.50},
                            "futures": {"volume_btc": 0.000001},
                        },
                    },
                }
            },
        }

        self.assertFalse(runtime._entry_quorum_ok(result, state, 1000.01))
        self.assertEqual(state.entry_tier_s_volume_quality["venues"], {})

    def test_stale_snapshot_fails_closed(self):
        runtime = self.make_runtime(True)
        state = SimpleNamespace()
        result = {
            "entry_mode": "NORMAL",
            "ts": 1000.0,
            "s_votes": {
                "S2_multi_venue_executed_flow": {
                    "status": "PASS",
                    "metrics": {
                        "ts": 1000.0,
                        "volume_floor_btc": 0.02,
                        "supporters": ["spot", "futures"],
                        "venues": {
                            "spot": {"volume_btc": 0.20},
                            "futures": {"volume_btc": 0.30},
                        },
                    },
                }
            },
        }

        self.assertFalse(runtime._entry_quorum_ok(result, state, 1001.0))
        self.assertFalse(state.entry_tier_s_volume_quality["snapshot_fresh"])

    def test_venue_specific_floor_accepts_material_coinbase_not_dust_futures(self):
        runtime = self.make_runtime(True)
        state = SimpleNamespace()
        result = {
            "entry_mode": "NORMAL",
            "ts": 1000.0,
            "s_votes": {
                "S2_multi_venue_executed_flow": {
                    "status": "PASS",
                    "metrics": {
                        "ts": 1000.0,
                        "volume_floor_btc": 0.002,
                        "volume_floor_btc_by_venue": {
                            "spot": 0.015, "futures": 0.15, "coinbase": 0.002,
                        },
                        "supporters": ["spot", "futures", "coinbase"],
                        "venues": {
                            "spot": {"volume_btc": 0.020},
                            "futures": {"volume_btc": 0.020},
                            "coinbase": {"volume_btc": 0.003},
                        },
                    },
                }
            },
        }
        self.assertTrue(runtime._entry_quorum_ok(result, state, 1000.01))
        self.assertEqual(
            set(state.entry_tier_s_volume_quality["venues"]),
            {"spot", "coinbase"},
        )


if __name__ == "__main__":
    unittest.main()
