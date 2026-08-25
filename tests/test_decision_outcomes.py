import unittest

from recorder.decision_outcomes import DecisionOutcomeTracker


def decision_event(cycle_id="d1", start_ms=1_000, episode_id=None,
                   qualified_now=False, council_ready=False,
                   reference_price=100.0, decision="WAIT",
                   taxonomy="WAIT_CHASE"):
    return {
        "stream": "bot_event",
        "code_version": "code-v1",
        "config_version": "config-v1",
        "event_time_ms": start_ms,
        "payload": {
            "event": "DECISION_EVALUATED",
            "cycle_id": cycle_id,
            "causal_episode_id": episode_id,
            "canonical_opportunity": {
                "causal_episode_id": episode_id,
                "qualified_now": qualified_now,
                "qualification_transition": qualified_now,
            },
            "vote_status": {
                "S1_cross_venue_price_acceptance": (
                    "PASS" if council_ready else "WAIT"
                ),
                "S2_multi_venue_executed_flow": (
                    "PASS" if council_ready else "WAIT"
                ),
            },
            "decision_record": {
                "cycle_id": cycle_id,
                "causal_episode_id": episode_id,
                "output": {
                    "decision": decision,
                    "reason": "WAIT_CHASE_PERP_AHEAD_OF_SPOT",
                    "miss_taxonomy": taxonomy,
                    "failed_gates": [taxonomy],
                },
                "counterfactual": {
                    "eligible": True,
                    "reference_price": reference_price,
                    "side": "LONG",
                    "hard_sl_bps": 50.0,
                },
            },
        },
    }


def trade(event_ms, sequence, price, high=None, low=None, previous=None):
    return {
        "stream": "futures_trade_100ms",
        "event_time_ms": event_ms,
        "sequence_start": sequence,
        "sequence_end": sequence,
        "previous_sequence": previous,
        "payload": {
            "last_event_time_ms": event_ms,
            "last_price": price,
            "high": price if high is None else high,
            "low": price if low is None else low,
        },
    }


def daily_lock_skip(cycle_id="risk-1", start_ms=1_000):
    return {
        "stream": "bot_event",
        "code_version": "code-v1",
        "config_version": "config-v1",
        "event_time_ms": start_ms,
        "payload": {
            "event": "ENTRY_SKIPPED",
            "cycle_id": cycle_id,
            "reason": "DAILY_LOSS_LOCKED",
            "miss_taxonomy": "RISK_DAILY_LOCK",
            "failed_gates": ["RISK_DAILY_LOCK"],
            "counterfactual": {
                "eligible": True,
                "reference_price": 100.0,
                "side": "SHORT",
                "hard_sl_bps": 50.0,
            },
        },
    }


def persistent_candidate_event(start_ms=1_000, side="SHORT"):
    row = decision_event(start_ms=start_ms)
    decision = row["payload"]["decision_record"]
    decision["counterfactual"].update({
        "eligible": False,
        "side": "ABSTAIN",
        "frozen_economics": {
            "execution_style": "MAKER",
            "cost_budget_bps": 8.5,
            "minimum_net_edge_bps": 2.0,
            "commission_verified": True,
        },
    })
    row["payload"]["persistent_metaorder_shadow"] = {
        "status": side + "_CANDIDATE",
        "candidate_side": side,
        "candidate_id": "pmeta:%s:%d" % (side, start_ms),
        "candidate_started_at_ms": start_ms,
        "transition": True,
        "authority": False,
        "sides": {
            side: {
                "status": "PERSISTENT_METAORDER_CANDIDATE",
                "cash_candidates": ["binance_spot"],
                "futures_follow": True,
            },
        },
    }
    return row


class DecisionOutcomeTrackerTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.tracker = DecisionOutcomeTracker(
            lambda stream, payload, event_time_ms=None: self.rows.append(
                (stream, payload, event_time_ms)
            )
        )

    def test_emits_directional_excursions_and_hypothetical_stop(self):
        self.tracker.observe(decision_event())
        self.tracker.observe(trade(6_000, 10, 101.0, high=102.0, low=99.0))
        self.assertEqual(len(self.rows), 1)
        stream, payload, _ = self.rows[0]
        self.assertEqual(stream, "decision_counterfactual")
        self.assertEqual(payload["window_seconds"], 5)
        self.assertEqual(payload["signed_close_bps"], 100.0)
        self.assertEqual(payload["max_favorable_excursion_bps"], 200.0)
        self.assertEqual(payload["max_adverse_excursion_bps"], 100.0)
        self.assertTrue(payload["hypothetical_hard_sl_hit"])

    def test_carries_frozen_cost_into_economic_miss_screen(self):
        event = decision_event(episode_id="episode-economic")
        event["payload"]["decision_record"]["counterfactual"]["frozen_economics"] = {
            "execution_style": "MAKER",
            "cost_budget_bps": 8.5,
            "minimum_net_edge_bps": 2.0,
            "commission_verified": True,
        }
        self.tracker.observe(event)
        self.tracker.observe(trade(6_000, 10, 100.20, high=100.20, low=100.0))
        payload = self.rows[0][1]
        self.assertEqual(payload["economic_miss_threshold_bps"], 10.5)
        self.assertTrue(payload["economic_miss_screen_passed"])
        self.assertTrue(payload["economic_miss_eligible"])
        self.assertFalse(payload["diagnostic_move_screen_passed"])
        self.assertEqual(payload["frozen_economics"]["execution_style"], "MAKER")

    def test_decision_cycles_are_one_diagnostic_wave_not_economic_misses(self):
        for cycle_id, start_ms, reference in (
            ("cycle-a", 1_000, 100.0),
            ("cycle-b", 1_100, 100.01),
            ("cycle-c", 1_200, 100.02),
        ):
            event = decision_event(
                cycle_id=cycle_id, start_ms=start_ms,
                reference_price=reference,
            )
            event["payload"]["decision_record"]["counterfactual"][
                "frozen_economics"
            ] = {
                "execution_style": "MAKER",
                "cost_budget_bps": 8.5,
                "minimum_net_edge_bps": 2.0,
                "commission_verified": True,
            }
            self.tracker.observe(event)

        self.assertEqual(list(self.tracker.pending), ["diag:LONG:1000"])
        self.tracker.observe(trade(6_200, 10, 100.20, high=100.20, low=100.0))
        payload = self.rows[0][1]
        self.assertEqual(payload["sample_scope"], "DECISION_CYCLE")
        self.assertEqual(payload["anchor_role"], "DECISION_CYCLE")
        self.assertEqual(payload["episode_anchor_rank"], 0)
        self.assertEqual(payload["diagnostic_wave_id"], "diag:LONG:1000")
        self.assertFalse(payload["economic_miss_eligible"])
        self.assertFalse(payload["economic_miss_screen_passed"])
        self.assertTrue(payload["diagnostic_move_screen_passed"])

    def test_persistent_candidate_gets_non_authoritative_outcomes(self):
        self.tracker.observe(persistent_candidate_event())
        self.assertEqual(list(self.tracker.pending), ["pmeta:SHORT:1000"])
        self.tracker.observe(trade(6_000, 10, 99.0, high=100.0, low=98.0))
        payload = self.rows[0][1]
        self.assertEqual(payload["sample_scope"], "PERSISTENT_METAORDER_SHADOW")
        self.assertEqual(
            payload["persistent_metaorder_candidate_id"], "pmeta:SHORT:1000",
        )
        self.assertFalse(payload["authority"])
        self.assertFalse(payload["economic_miss_eligible"])
        self.assertFalse(payload["economic_miss_screen_passed"])
        self.assertTrue(payload["diagnostic_move_screen_passed"])

    def test_sequence_gap_invalidates_instead_of_bridging(self):
        self.tracker.observe(decision_event())
        self.tracker.observe(trade(2_000, 10, 100.0))
        self.tracker.observe(trade(6_000, 12, 101.0, previous=10))
        self.assertEqual(len(self.rows), 1)
        payload = self.rows[0][1]
        self.assertFalse(payload["valid"])
        self.assertEqual(
            payload["invalid_reason"], "FUTURES_EXECUTED_FLOW_SEQUENCE_GAP"
        )
        self.assertEqual(len(self.tracker.pending), 0)

    def test_non_directional_bias_wait_is_not_registered(self):
        row = decision_event()
        row["payload"]["decision_record"]["counterfactual"]["eligible"] = False
        self.tracker.observe(row)
        self.assertEqual(len(self.tracker.pending), 0)

    def test_journal_tail_delay_backfills_already_received_excursion(self):
        self.tracker.observe(trade(1_500, 10, 99.0, high=101.0, low=98.0))
        self.tracker.observe(decision_event(start_ms=1_000))
        self.tracker.observe(trade(6_000, 11, 100.5, previous=10))
        payload = self.rows[0][1]
        self.assertEqual(payload["max_favorable_excursion_bps"], 100.0)
        self.assertEqual(payload["max_adverse_excursion_bps"], 200.0)

    def test_daily_lock_skip_is_tracked_as_execution_miss(self):
        self.tracker.observe(daily_lock_skip())
        self.assertIn("risk-1::EXECUTION", self.tracker.pending)
        tracker = self.tracker.pending["risk-1::EXECUTION"]
        self.assertEqual(tracker["miss_taxonomy"], "RISK_DAILY_LOCK")
        self.assertEqual(tracker["failed_gates"], ["RISK_DAILY_LOCK"])
        self.assertEqual(tracker["strategy_code_version"], "code-v1")
        self.assertEqual(tracker["strategy_config_version"], "config-v1")
        self.tracker.observe(trade(6_000, 10, 99.0, high=101.0, low=98.0))
        payload = self.rows[0][1]
        self.assertEqual(payload["miss_taxonomy"], "RISK_DAILY_LOCK")
        self.assertEqual(payload["signed_close_bps"], 100.0)
        self.assertEqual(payload["sample_scope"], "EXECUTION_EVENT")
        self.assertTrue(payload["economic_miss_eligible"])

    def test_filled_then_flattened_is_one_execution_outcome(self):
        event = daily_lock_skip(cycle_id="abort-1", start_ms=1_000)
        payload = event["payload"]
        payload.update({
            "event": "ENTRY_FILLED_THEN_FLATTENED",
            "causal_episode_id": "episode-abort",
            "miss_taxonomy": "LIVE_FILLED_THEN_FLATTENED",
            "failed_gates": ["POST_FILL_RISK_REJECTED"],
        })
        self.tracker.observe(event)
        self.tracker.observe(event)
        self.assertEqual(list(self.tracker.pending), [
            "episode-abort::EXECUTION"
        ])
        self.tracker.observe(trade(6_000, 10, 101.0))
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(
            self.rows[0][1]["miss_taxonomy"],
            "LIVE_FILLED_THEN_FLATTENED",
        )

    def test_episode_keeps_origin_and_exact_qualified_anchor(self):
        self.tracker.observe(decision_event(
            "early", start_ms=1_000, episode_id="tier-s:7",
            reference_price=100.0,
        ))
        self.tracker.observe(decision_event(
            "qualified", start_ms=2_000, episode_id="tier-s:7",
            qualified_now=True, council_ready=True, reference_price=101.0,
        ))
        self.assertEqual(
            list(self.tracker.pending), ["tier-s:7", "tier-s:7::GO"]
        )
        self.assertEqual(
            self.tracker.pending["tier-s:7::GO"]["cycle_id"], "qualified"
        )
        self.tracker.observe(trade(7_000, 10, 102.0, high=103.0, low=100.0))
        self.assertEqual(len(self.rows), 2)
        payload = next(row[1] for row in self.rows if row[1]["anchor_role"] == "QUALIFIED")
        self.assertEqual(payload["cycle_id"], "qualified")
        self.assertEqual(payload["causal_episode_id"], "tier-s:7")
        self.assertEqual(payload["sample_scope"], "CAUSAL_EPISODE_GO_ANCHOR")
        self.assertEqual(payload["episode_anchor_rank"], 4)
        self.assertAlmostEqual(payload["signed_close_bps"], 99.009901)

    def test_completed_origin_still_allows_later_exact_go_anchor(self):
        self.tracker.observe(decision_event(
            "first", episode_id="tier-s:8",
        ))
        for event_ms, sequence in ((6_000, 10), (16_000, 11),
                                   (31_000, 12), (61_000, 13)):
            self.tracker.observe(trade(
                event_ms, sequence, 101.0,
                previous=None if sequence == 10 else sequence - 1,
            ))
        self.assertIn("tier-s:8", self.tracker.closed)
        before = len(self.rows)
        self.tracker.observe(decision_event(
            "late", start_ms=62_000, episode_id="tier-s:8",
            qualified_now=True, council_ready=True, decision="GO",
            taxonomy="EDGE_COST_FAIL",
        ))
        self.assertIn("tier-s:8::GO", self.tracker.pending)
        self.assertEqual(len(self.rows), before)

    def test_cost_failed_go_gets_exact_anchor_after_origin_window(self):
        self.tracker.observe(decision_event(
            "origin", episode_id="tier-s:9", reference_price=100.0,
        ))
        self.tracker.observe(trade(6_000, 10, 100.5))
        self.tracker.observe(decision_event(
            "edge-fail", start_ms=7_000, episode_id="tier-s:9",
            reference_price=101.0, decision="GO", taxonomy="EDGE_COST_FAIL",
        ))
        self.assertIn("tier-s:9::GO", self.tracker.pending)
        self.assertEqual(
            self.tracker.pending["tier-s:9::GO"]["reference_price"], 101.0
        )
        self.assertEqual(
            self.tracker.pending["tier-s:9::GO"]["anchor_role"], "GO_CANDIDATE"
        )


if __name__ == "__main__":
    unittest.main()
