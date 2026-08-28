import unittest
from types import SimpleNamespace
from unittest.mock import patch

from loi_he_thong import execution_causal_revalidation as recheck
from loi_he_thong import ignition_core, ignition_signals, verified_cost_model


def material(venue, bucket, side, acceleration=1.0, price=None):
    sign = 1.0 if side == "LONG" else -1.0
    row = {
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
    if price is not None:
        row["price"] = float(price)
    return row


def fixture():
    engine = ignition_signals.SignalEngine()
    for venue in engine.venues.values():
        venue.epoch = 1
        venue.clock_valid = True
    state = SimpleNamespace(
        _ignition_signal_engine=engine,
        canonical_reserved_context={},
        bias_state="LONG",
        bias_confidence=0.8,
        bias_updated_at=10.0,
        execution_best_bid=99.9,
        execution_best_ask=100.1,
        execution_price_time=10.0,
        best_bid=99.9,
        best_ask=100.1,
        atr_1m=1.0,
        atr_1m_updated_at=10.0,
        mainnet_maker_fee_bps=2.0,
        mainnet_taker_fee_bps=5.0,
        mainnet_commission_verified=True,
    )
    result = {
        "ts": 9.5,
        "canonical_opportunity_id": 9,
        "causal_episode_id": "episode-9",
        "ignition": {
            "cash_venues": ["binance_spot"],
            "venue_anchor_prices": {"binance_spot": 100.0},
            "phase_measurement": {"precursor_cash_displacement_bps": 0.0},
            "proof_type": "METAORDER_CONTINUATION",
            "proof_venue": "binance_spot",
            "bias_snapshot": {
                "direction": "LONG", "confidence": 0.8,
                "updated_at": 9.0,
            },
            "current_cash_conversion": {
                "confirmed": True,
                "venues": {"binance_spot": {
                    "receive_time_ms": 9_800, "epoch": 1,
                    "imbalance": 0.8, "price_conversion_bps": 0.3,
                }},
            },
            "clock_quality": {
                "binance_spot": {"epoch": 1},
                "futures": {"epoch": 1},
            },
        },
    }
    basis, dependencies, proof_hash = ignition_core._freeze_authority_proof(
        result["ignition"], "LONG", "METAORDER_CONTINUATION", "episode-9",
    )
    result.update({
        "authority_basis": basis,
        "authority_dependencies": dependencies,
        "authority_proof_hash": proof_hash,
    })
    state.canonical_reserved_context = {
        "opportunity_id": 9,
        "causal_episode_id": "episode-9",
        "epochs": {"binance_spot": 1, "futures": 1},
        "authority_basis": basis,
        "authority_dependencies": dependencies,
        "authority_proof_hash": proof_hash,
    }
    result["execution_cost_contract"] = (
        verified_cost_model.freeze_execution_cost_contract(result, state)
    )
    return state, result


class ExecutionCausalRevalidationTests(unittest.TestCase):
    def test_pass_records_go_to_submit_timing_and_cash_age(self):
        state, result = fixture()

        ok, reason, detail = recheck.validate_submit(
            state, "LONG", result, 10.0,
        )

        self.assertTrue(ok, (reason, detail))
        self.assertEqual(detail["decision_to_submit_ms"], 500.0)
        self.assertEqual(detail["flow_state_at_GO"], "UNKNOWN")
        self.assertEqual(detail["flow_state_at_submit"], "UNKNOWN")
        self.assertEqual(detail["cash_age_at_submit"], 200)
        self.assertEqual(
            detail["cash_age_by_venue_ms"], {"binance_spot": 200}
        )

    def test_timing_telemetry_flags_confirmed_flow_that_fades(self):
        state, result = fixture()
        result["authority_dependencies"] = dict(
            result["authority_dependencies"],
            flow_state_at_go="CONTINUING_CONFIRMED",
        )
        with patch.object(
            ignition_core, "causal_wave_snapshot",
            return_value={
                "version": "CAUSAL_WAVE_SNAPSHOT_V1",
                "flow_efficiency_state": "FADING",
            },
        ):
            detail = recheck._submit_timing_telemetry(
                state, "LONG", result, 10.0,
            )

        self.assertEqual(detail["flow_state_at_GO"], "CONTINUING_CONFIRMED")
        self.assertEqual(detail["flow_state_at_submit"], "FADING")
        self.assertTrue(detail["flow_decayed_before_submit"])

    def test_transition_proof_does_not_recheck_opposite_slow_bias(self):
        state, result = fixture()
        state.bias_state = "SHORT"
        ignition = dict(result["ignition"])
        ignition["cash_venues"] = ["binance_spot", "coinbase_spot"]
        ignition["current_cash_conversion"] = {
            "confirmed": True,
            "venues": {
                "binance_spot": {
                    "receive_time_ms": 9_800, "epoch": 1,
                    "imbalance": 0.8, "price_conversion_bps": 0.3,
                },
                "coinbase_spot": {
                    "receive_time_ms": 9_850, "epoch": 1,
                    "imbalance": 0.7, "price_conversion_bps": 0.25,
                },
            },
        }
        ignition["clock_quality"] = {
            "binance_spot": {"epoch": 1},
            "coinbase_spot": {"epoch": 1},
            "futures": {"epoch": 1},
        }
        ignition["transition_confirmed"] = True
        ignition["transition_authority"] = {
            "status": "REVERSAL_CONFIRMED", "side": "LONG",
            "background_side": "SHORT",
            "old_side_failure_confirmed": True,
            "old_failure_venues": ["binance_spot"],
            "new_side_cash_control_confirmed": True,
            "cash_synchronous_transition": True,
            "accepted_cash_venues": ["binance_spot", "coinbase_spot"],
            "cash_acceptance_span_ms": 50,
            "hard_contradiction": False,
        }
        basis, dependencies, proof_hash = ignition_core._freeze_authority_proof(
            ignition, "LONG", "METAORDER_CONTINUATION", "episode-9",
        )
        result.update({
            "ignition": ignition,
            "authority_basis": basis,
            "authority_dependencies": dependencies,
            "authority_proof_hash": proof_hash,
        })
        state.canonical_reserved_context.update({
            "epochs": {
                "binance_spot": 1, "coinbase_spot": 1, "futures": 1,
            },
            "authority_basis": basis,
            "authority_dependencies": dependencies,
            "authority_proof_hash": proof_hash,
        })

        ok, reason, detail = recheck.validate_submit(
            state, "LONG", result, 10.0,
        )

        self.assertTrue(ok, (reason, detail))
        self.assertEqual(basis, "TRANSITION_CONFIRMED")
        self.assertEqual(detail["authority_basis"], "TRANSITION_CONFIRMED")

    def test_mutated_authority_dependencies_fail_closed(self):
        state, result = fixture()
        result["authority_dependencies"] = dict(
            result["authority_dependencies"], side="SHORT"
        )
        ok, reason, _ = recheck.validate_submit(
            state, "LONG", result, 10.0,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "RESERVED_AUTHORITY_DEPENDENCIES_CHANGED")

    def test_current_cash_dependency_expires_without_rerunning_strategy(self):
        state, result = fixture()
        ok, reason, _ = recheck.validate_submit(
            state, "LONG", result, 10.5,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "CURRENT_CASH_AUTHORITY_EXPIRED")

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
            material("binance_spot", 9600, "SHORT"),
            material("binance_spot", 9700, "SHORT"),
        ])
        ok, reason, _ = recheck.validate_submit(state, "LONG", result, 10.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "POST_PROOF_OPPOSING_FLOW_2_BUCKETS")

    def test_cash_price_reversal_needs_independent_opposing_flow_bucket(self):
        state, result = fixture()
        history = state._ignition_signal_engine.venues["binance_spot"].history
        price_row = material("binance_spot", 9600, "LONG")
        price_row["price_conversion_bps"] = -0.20
        flow_row = material("binance_spot", 9700, "SHORT")
        flow_row["price_conversion_bps"] = 0.0
        history.extend([price_row, flow_row])
        ok, reason, _ = recheck.validate_submit(state, "LONG", result, 10.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "POST_PROOF_CASH_PRICE_FLOW_REVERSAL")

    def test_distant_price_and_flow_cannot_be_stitched_into_reversal(self):
        state, result = fixture()
        history = state._ignition_signal_engine.venues["binance_spot"].history
        flow_row = material("binance_spot", 9600, "SHORT")
        flow_row["price_conversion_bps"] = 0.0
        price_row = material("binance_spot", 10_000, "LONG")
        price_row["price_conversion_bps"] = -0.20
        history.extend([flow_row, price_row])

        ok, reason, _ = recheck.validate_submit(state, "LONG", result, 10.1)

        self.assertTrue(ok)
        self.assertEqual(reason, "PASS")

    def test_coherent_reversal_reports_both_buckets_and_gap(self):
        state, result = fixture()
        history = state._ignition_signal_engine.venues["binance_spot"].history
        price_row = material("binance_spot", 9600, "LONG")
        price_row["price_conversion_bps"] = -0.20
        flow_row = material("binance_spot", 9800, "SHORT")
        flow_row["price_conversion_bps"] = 0.0
        history.extend([price_row, flow_row])

        ok, reason, detail = recheck.validate_submit(
            state, "LONG", result, 10.0
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "POST_PROOF_CASH_PRICE_FLOW_REVERSAL")
        self.assertEqual(detail["price_bucket"], 9600)
        self.assertEqual(detail["flow_bucket"], 9800)
        self.assertEqual(detail["coherence_gap_ms"], 200)

    def test_maker_release_requires_cash_persistence_and_futures_response(self):
        state, result = fixture()
        cash = state._ignition_signal_engine.venues["binance_spot"].history
        futures = state._ignition_signal_engine.venues["futures"].history
        cash.extend([
            material("binance_spot", 9600, "LONG", price=100.002),
            material("binance_spot", 9700, "LONG", price=100.004),
        ])
        ok, reason, _ = recheck.maker_ttl_release(
            state, "LONG", result, 10.0, 9.5
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "CURRENT_RELEASE_NOT_PROVED")
        futures.append(material("futures", 9700, "LONG"))
        ok, reason, detail = recheck.maker_ttl_release(
            state, "LONG", result, 10.0, 9.5
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "CURRENT_RELEASE_PASS")
        self.assertEqual(detail["cash_venue"], "binance_spot")

    def test_maker_release_ignores_evidence_before_order_placement(self):
        state, result = fixture()
        cash = state._ignition_signal_engine.venues["binance_spot"].history
        futures = state._ignition_signal_engine.venues["futures"].history
        cash.extend([
            material("binance_spot", 9500, "LONG", price=100.002),
            material("binance_spot", 9600, "LONG", price=100.004),
        ])
        futures.append(material("futures", 9600, "LONG"))
        ok, reason, _ = recheck.maker_ttl_release(
            state, "LONG", result, 10.0, 9.7
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "CURRENT_RELEASE_NOT_PROVED")

    def test_maker_release_rejects_currently_consumed_impulse(self):
        state, result = fixture()
        cash = state._ignition_signal_engine.venues["binance_spot"].history
        futures = state._ignition_signal_engine.venues["futures"].history
        cash.extend([
            material("binance_spot", 9600, "LONG", price=100.20),
            material("binance_spot", 9700, "LONG", price=100.40),
        ])
        futures.append(material("futures", 9700, "LONG"))
        ok, reason, detail = recheck.maker_ttl_release(
            state, "LONG", result, 10.0, 9.5
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "CURRENT_IMPULSE_ALREADY_CONSUMED")
        self.assertGreater(detail["consumed_fraction"], 0.35)

    def test_straddling_bucket_is_not_post_decision_evidence(self):
        state, result = fixture()
        result["ts"] = 9.55
        cash = state._ignition_signal_engine.venues["binance_spot"].history
        cash.extend([
            material("binance_spot", 9500, "SHORT"),
            material("binance_spot", 9600, "SHORT"),
        ])
        ok, reason, _ = recheck.validate_submit(state, "LONG", result, 10.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "PASS")

    def test_epoch_change_fails_closed(self):
        state, result = fixture()
        state._ignition_signal_engine.venues["futures"].epoch = 2
        ok, reason, _ = recheck.validate_submit(state, "LONG", result, 10.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "EXECUTED_FLOW_EPOCH_RESET")


if __name__ == "__main__":
    unittest.main()
