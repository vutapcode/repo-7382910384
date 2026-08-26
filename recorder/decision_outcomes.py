"""Bounded counterfactual outcomes for recorded Tier-S misses.

This is recorder-only evidence.  It never feeds the live strategy. Outcomes use
the first causally received Futures aggTrade batch at/after each horizon and are
invalidated on a sequence discontinuity instead of bridging a data gap.
"""

from collections import deque, OrderedDict


WINDOWS_MS = (5_000, 15_000, 30_000, 60_000)
PERSISTENT_WINDOWS_MS = WINDOWS_MS + (180_000, 300_000, 900_000)
MAX_PENDING = 512
MAX_CLOSED = 2_048
DIAGNOSTIC_WAVE_GAP_MS = 5_000
DIAGNOSTIC_WAVE_PRICE_BPS = 3.0
VERSION = "DECISION_COUNTERFACTUAL_V6_FINAL_ADJUDICATION"
ADJUDICATION_VERSION = "ECONOMIC_MISS_ADJUDICATION_V1"


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class DecisionOutcomeTracker:
    def __init__(self, emit):
        self.emit = emit
        self.pending = OrderedDict()
        self.closed = OrderedDict()
        self.last_futures_sequence = None
        # DecisionTap tails the journal asynchronously. Keep enough already
        # received prices to backfill the sub-second tap delay without lookahead.
        self.recent_futures = deque(maxlen=1_200)
        self.last_gap_event_ms = 0
        self.last_futures_event_ms = 0
        # One bounded diagnostic identity per side.  These records are useful
        # for research before Ignition exists, but never count as economic
        # misses or independent opportunities.
        self.diagnostic_waves = {}
        # Recorder-only proximity clusters are diagnostics only. Economic miss
        # identity follows canonical episode identity; proximity never dedupes.
        self.economic_waves = {}
        self.episode_wave_ids = OrderedDict()
        self.adjudicated_waves = OrderedDict()

    @staticmethod
    def _candidate(record):
        if str(record.get("stream", "")) != "bot_event":
            return None
        payload = record.get("payload") or {}
        event = str(payload.get("event", ""))
        if event == "DECISION_EVALUATED":
            decision = payload.get("decision_record") or {}
            output = decision.get("output") or {}
            counterfactual = decision.get("counterfactual") or {}
            opportunity = payload.get("canonical_opportunity") or {}
            qualified_now = bool(
                opportunity.get("qualified_now")
                or opportunity.get("qualification_transition")
                or (
                    (output.get("decision") or payload.get("decision")) == "GO"
                    and bool(output.get("quorum_ok", payload.get("quorum_ok")))
                )
            )
            votes = payload.get("vote_status") or {}
            council_ready = bool(
                votes.get("S1_cross_venue_price_acceptance") == "PASS"
                and votes.get("S2_multi_venue_executed_flow") == "PASS"
            )
            is_go = str(output.get("decision") or payload.get("decision") or "").upper() == "GO"
            episode_id = str(
                decision.get("causal_episode_id")
                or payload.get("causal_episode_id") or ""
            )
            if not episode_id:
                rank, anchor_role = 0, "DECISION_CYCLE"
            elif qualified_now:
                rank, anchor_role = 4, "QUALIFIED"
            elif is_go:
                rank, anchor_role = 3, "GO_CANDIDATE"
            elif council_ready:
                rank, anchor_role = 2, "COUNCIL_READY"
            else:
                rank, anchor_role = 1, "EPISODE_ORIGIN"
            return {
                "cycle_id": decision.get("cycle_id") or payload.get("cycle_id"),
                "causal_episode_id": (
                    episode_id or None
                ),
                "miss_taxonomy": output.get("miss_taxonomy") or payload.get("miss_taxonomy"),
                "failed_gates": output.get("failed_gates") or payload.get("failed_gates") or [],
                "counterfactual": counterfactual,
                "frozen_economics": dict(
                    counterfactual.get("frozen_economics") or {}
                ),
                "decision": output.get("decision") or payload.get("decision"),
                "reason": output.get("reason") or payload.get("reason"),
                "taxonomy_version": decision.get("taxonomy_version"),
                "strategy_code_version": decision.get("strategy_code_version"),
                "strategy_config_version": decision.get("strategy_config_version"),
                "anchor_rank": rank,
                "anchor_role": anchor_role,
                "qualified_now": qualified_now,
            }
        if event in (
            "ENTRY_SKIPPED",
            "SHADOW_MAKER_CANCELED",
            "ENTRY_FILLED_THEN_FLATTENED",
        ):
            return {
                "cycle_id": payload.get("cycle_id"),
                "causal_episode_id": payload.get("causal_episode_id"),
                "miss_taxonomy": payload.get("miss_taxonomy"),
                "failed_gates": payload.get("failed_gates") or [
                    payload.get("miss_taxonomy")
                ],
                "counterfactual": payload.get("counterfactual") or {},
                "frozen_economics": dict(
                    (payload.get("counterfactual") or {}).get("frozen_economics")
                    or payload.get("frozen_economics") or {}
                ),
                "decision": (
                    "ABORTED_EXECUTION"
                    if event == "ENTRY_FILLED_THEN_FLATTENED" else "SKIP"
                ),
                "reason": payload.get("reason"),
                "taxonomy_version": payload.get("taxonomy_version"),
                "strategy_code_version": (
                    payload.get("strategy_code_version")
                    or record.get("code_version")
                ),
                "strategy_config_version": (
                    payload.get("strategy_config_version")
                    or record.get("config_version")
                ),
                # An execution-layer miss is the most actionable episode anchor.
                "anchor_rank": 5,
                "anchor_role": "EXECUTION",
                "qualified_now": False,
            }
        return None

    def _diagnostic_wave_id(self, side, start_ms, reference):
        previous = self.diagnostic_waves.get(side)
        if previous is not None:
            gap = int(start_ms) - int(previous["last_ms"])
            anchor = float(previous["anchor_price"])
            distance_bps = abs(float(reference) - anchor) / anchor * 10_000.0
            if (
                0 <= gap <= DIAGNOSTIC_WAVE_GAP_MS
                and distance_bps <= DIAGNOSTIC_WAVE_PRICE_BPS
            ):
                previous["last_ms"] = int(start_ms)
                return str(previous["wave_id"])
        wave_id = "diag:%s:%d" % (side, int(start_ms))
        self.diagnostic_waves[side] = {
            "wave_id": wave_id,
            "anchor_price": float(reference),
            "started_at_ms": int(start_ms),
            "last_ms": int(start_ms),
        }
        return wave_id

    def _economic_cluster_id(self, side, start_ms, reference):
        previous = self.economic_waves.get(side)
        if previous is not None:
            gap = int(start_ms) - int(previous["last_ms"])
            anchor = float(previous["anchor_price"])
            distance_bps = abs(float(reference) - anchor) / anchor * 10_000.0
            if (
                0 <= gap <= DIAGNOSTIC_WAVE_GAP_MS
                and distance_bps <= DIAGNOSTIC_WAVE_PRICE_BPS
            ):
                previous["last_ms"] = int(start_ms)
                return str(previous["cluster_id"])
        cluster_id = "cluster:%s:%d" % (side, int(start_ms))
        self.economic_waves[side] = {
            "cluster_id": cluster_id,
            "anchor_price": float(reference),
            "started_at_ms": int(start_ms),
            "last_ms": int(start_ms),
        }
        return cluster_id

    def _economic_wave_id(self, episode_id, side, start_ms, reference):
        if episode_id and episode_id in self.episode_wave_ids:
            return self.episode_wave_ids[episode_id]
        wave_id = (
            "economic:%s:%s" % (side, episode_id)
            if episode_id
            else "economic:%s:%d" % (side, int(start_ms))
        )
        if episode_id:
            self.episode_wave_ids[episode_id] = wave_id
            self.episode_wave_ids.move_to_end(episode_id)
            while len(self.episode_wave_ids) > MAX_CLOSED:
                self.episode_wave_ids.popitem(last=False)
        return wave_id

    @staticmethod
    def _guardian_counterfactual_net(candidate):
        counterfactual = candidate.get("counterfactual") or {}
        guardian = counterfactual.get("guardian_counterfactual") or {}
        return _f(
            guardian.get("net_pnl_bps_after_frozen_cost"), None
        )

    def _emit_final_adjudication(self, tracker, event_ms):
        if not tracker.get("economic_miss_eligible"):
            return
        wave_id = str(tracker.get("economic_wave_id") or "")
        primary = self.adjudicated_waves.get(wave_id)
        if primary is not None:
            classification = "DUPLICATE_EPISODE"
            missing = []
        else:
            counterfactual = tracker.get("counterfactual") or {}
            screen = bool(tracker.get("economic_screen_ever", False))
            causal = counterfactual.get("causal_continuity_confirmed")
            fill = counterfactual.get("fill_feasible")
            feed_clean = counterfactual.get("feed_clean")
            guardian_net = self._guardian_counterfactual_net(tracker)
            missing = []
            if causal is not True:
                missing.append("CAUSAL_CONTINUITY")
            if fill is not True:
                missing.append("EXECUTABLE_FILL")
            if feed_clean is not True:
                missing.append("CLEAN_FEED")
            if guardian_net is None:
                missing.append("GUARDIAN_COUNTERFACTUAL")
            elif guardian_net <= 0.0:
                missing.append("POSITIVE_NET_AFTER_FROZEN_COST")
            if not screen:
                classification = "GOOD_REJECT"
            elif causal is False:
                classification = "DELAYED_UNRELATED_MOVE"
            elif not missing:
                classification = "ECONOMIC_MISS_CONFIRMED"
            else:
                classification = "MISS_SCREEN_ONLY"
            self.adjudicated_waves[wave_id] = {
                "tracking_key": tracker.get("tracking_key"),
                "causal_episode_id": tracker.get("causal_episode_id"),
                "classification": classification,
            }
            self.adjudicated_waves.move_to_end(wave_id)
            while len(self.adjudicated_waves) > MAX_CLOSED:
                self.adjudicated_waves.popitem(last=False)
        self.emit(
            "decision_miss_adjudication",
            {
                "version": ADJUDICATION_VERSION,
                "authority": False,
                "cycle_id": tracker.get("cycle_id"),
                "causal_episode_id": tracker.get("causal_episode_id"),
                "economic_wave_id": wave_id or None,
                "economic_cluster_id": tracker.get("economic_cluster_id"),
                "sample_scope": tracker.get("sample_scope"),
                "anchor_role": tracker.get("anchor_role"),
                "classification": classification,
                "economic_miss_confirmed": (
                    classification == "ECONOMIC_MISS_CONFIRMED"
                ),
                "raw_screen_passed": bool(
                    tracker.get("economic_screen_ever", False)
                ),
                "missing_confirmation": missing,
                "primary_wave_tracker": primary,
                "strategy_code_version": tracker.get("strategy_code_version"),
                "strategy_config_version": tracker.get("strategy_config_version"),
                "adjudicated_at_seconds": int(
                    (tracker.get("windows_ms") or WINDOWS_MS)[-1] // 1000
                ),
            },
            event_time_ms=int(event_ms),
        )

    @staticmethod
    def _persistent_candidate(record):
        if str(record.get("stream", "")) != "bot_event":
            return None
        payload = record.get("payload") or {}
        if str(payload.get("event", "")) != "DECISION_EVALUATED":
            return None
        report = payload.get("persistent_metaorder_shadow") or {}
        side = str(report.get("candidate_side") or "").upper()
        if not report.get("transition") or side not in ("LONG", "SHORT"):
            return None
        decision = payload.get("decision_record") or {}
        source_cf = decision.get("counterfactual") or {}
        reference = _f(source_cf.get("reference_price"))
        start_ms = int(record.get("event_time_ms", 0) or 0)
        if reference <= 0.0 or start_ms <= 0:
            return None
        candidate_id = str(
            report.get("candidate_id")
            or "pmeta:%s:%d" % (side, start_ms)
        )
        counterfactual = dict(source_cf)
        counterfactual.update({
            "eligible": True,
            "side": side,
            "reference_price": reference,
        })
        return {
            "cycle_id": decision.get("cycle_id") or payload.get("cycle_id"),
            "causal_episode_id": None,
            "persistent_metaorder_candidate_id": candidate_id,
            "persistent_candidate_started_at_ms": int(
                report.get("candidate_started_at_ms") or start_ms
            ),
            "persistent_metaorder_evidence": dict(
                (report.get("sides") or {}).get(side) or {}
            ),
            "miss_taxonomy": "PERSISTENT_METAORDER_SHADOW_CANDIDATE",
            "failed_gates": [],
            "counterfactual": counterfactual,
            "frozen_economics": dict(
                counterfactual.get("frozen_economics") or {}
            ),
            "decision": "SHADOW_OBSERVATION",
            "reason": str(report.get("status") or "PERSISTENT_CANDIDATE"),
            "taxonomy_version": decision.get("taxonomy_version"),
            "strategy_code_version": decision.get("strategy_code_version"),
            "strategy_config_version": decision.get("strategy_config_version"),
            "anchor_rank": 0,
            "anchor_role": "PERSISTENT_METAORDER_CANDIDATE",
            "qualified_now": False,
            "authority": False,
        }

    def _register_candidate(self, record, candidate):
        cf = candidate.get("counterfactual") or {}
        cycle_id = str(candidate.get("cycle_id") or "")
        side = str(cf.get("side") or "").upper()
        reference = _f(cf.get("reference_price"))
        if (
            not cycle_id or not candidate.get("miss_taxonomy")
            or not cf.get("eligible") or side not in ("LONG", "SHORT")
            or reference <= 0.0
        ):
            return
        start_ms = int(record.get("event_time_ms", 0) or 0)
        if start_ms <= 0:
            return
        episode_id = str(candidate.get("causal_episode_id") or "")
        anchor_role = str(candidate.get("anchor_role") or "DECISION_CYCLE")
        diagnostic_wave_id = None
        persistent_id = str(
            candidate.get("persistent_metaorder_candidate_id") or ""
        )
        suffix = (
            "::EXECUTION" if episode_id and anchor_role == "EXECUTION"
            else "::GO" if episode_id and anchor_role in ("QUALIFIED", "GO_CANDIDATE")
            else ""
        )
        if persistent_id:
            sample_scope = "PERSISTENT_METAORDER_SHADOW"
            tracking_key = persistent_id
        elif episode_id:
            sample_scope = (
                "CAUSAL_EPISODE_EXECUTION"
                if suffix == "::EXECUTION"
                else "CAUSAL_EPISODE_GO_ANCHOR"
                if suffix == "::GO"
                else "CAUSAL_EPISODE_ORIGIN"
            )
            tracking_key = episode_id + suffix
        elif anchor_role == "EXECUTION":
            sample_scope = "EXECUTION_EVENT"
            tracking_key = cycle_id + "::EXECUTION"
        else:
            sample_scope = "DECISION_CYCLE"
            diagnostic_wave_id = self._diagnostic_wave_id(
                side, start_ms, reference,
            )
            tracking_key = diagnostic_wave_id
        if tracking_key in self.closed:
            return
        existing = self.pending.get(tracking_key)
        if existing is not None:
            candidate_rank = int(candidate.get("anchor_rank", 0) or 0)
            existing_rank = int(existing.get("anchor_rank", 0) or 0)
            better_anchor = candidate_rank > existing_rank
            if not better_anchor or existing.get("completed"):
                return
            self.pending.pop(tracking_key, None)
        economic_miss_eligible = bool(
            episode_id or sample_scope == "EXECUTION_EVENT"
        ) and sample_scope != "PERSISTENT_METAORDER_SHADOW"
        economic_wave_id = (
            self._economic_wave_id(episode_id, side, start_ms, reference)
            if economic_miss_eligible else None
        )
        economic_cluster_id = (
            self._economic_cluster_id(side, start_ms, reference)
            if economic_miss_eligible else None
        )
        self.pending[tracking_key] = {
            **candidate,
            "tracking_key": tracking_key,
            "sample_scope": sample_scope,
            "diagnostic_wave_id": diagnostic_wave_id,
            "economic_miss_eligible": economic_miss_eligible,
            "economic_wave_id": economic_wave_id,
            "economic_cluster_id": economic_cluster_id,
            "economic_screen_ever": False,
            "start_ms": start_ms,
            "side": side,
            "reference_price": reference,
            "hard_sl_bps": _f(cf.get("hard_sl_bps"), None),
            "high": reference,
            "low": reference,
            "last_price": reference,
            "completed": set(),
            # Long-horizon waves are intentionally limited to the stable
            # persistent-metaorder cohort. Keeping every 100 ms reject alive
            # for 15 minutes would waste the host CPU budget and distort the
            # dataset with repeated decision cycles.
            "windows_ms": (
                PERSISTENT_WINDOWS_MS
                if sample_scope == "PERSISTENT_METAORDER_SHADOW"
                else WINDOWS_MS
            ),
        }
        if self.last_gap_event_ms >= start_ms:
            tracker = self.pending.pop(tracking_key)
            self._emit_invalid(
                tracker, "FUTURES_EXECUTED_FLOW_SEQUENCE_GAP",
                self.last_gap_event_ms,
            )
            self._close_key(tracking_key)
            return
        tracker = self.pending[tracking_key]
        for event_ms, price, high, low in self.recent_futures:
            if event_ms < start_ms:
                continue
            tracker["high"] = max(float(tracker["high"]), high)
            tracker["low"] = min(float(tracker["low"]), low)
            tracker["last_price"] = price
        self.pending.move_to_end(tracking_key)
        while len(self.pending) > MAX_PENDING:
            dropped_key, dropped = self.pending.popitem(last=False)
            self._emit_invalid(dropped, "PENDING_CAPACITY_EXCEEDED", start_ms)
            self._close_key(dropped_key)

    def _register(self, record):
        candidate = self._candidate(record)
        if candidate is not None:
            self._register_candidate(record, candidate)
        persistent = self._persistent_candidate(record)
        if persistent is not None:
            self._register_candidate(record, persistent)

    def _close_key(self, tracking_key):
        self.closed[str(tracking_key)] = True
        self.closed.move_to_end(str(tracking_key))
        while len(self.closed) > MAX_CLOSED:
            self.closed.popitem(last=False)

    def _emit_invalid(self, tracker, reason, at_ms):
        self.emit(
            "decision_counterfactual",
            {
                "version": VERSION,
                "cycle_id": tracker.get("cycle_id"),
                "causal_episode_id": tracker.get("causal_episode_id"),
                "economic_cluster_id": tracker.get("economic_cluster_id"),
                "diagnostic_wave_id": tracker.get("diagnostic_wave_id"),
                "persistent_metaorder_candidate_id": tracker.get(
                    "persistent_metaorder_candidate_id"
                ),
                "sample_scope": tracker.get("sample_scope"),
                "episode_anchor_rank": tracker.get("anchor_rank"),
                "anchor_role": tracker.get("anchor_role"),
                "authority": bool(tracker.get("authority", False)),
                "economic_miss_eligible": bool(
                    tracker.get("economic_miss_eligible", False)
                ),
                "taxonomy_version": tracker.get("taxonomy_version"),
                "strategy_code_version": tracker.get("strategy_code_version"),
                "strategy_config_version": tracker.get("strategy_config_version"),
                "miss_taxonomy": tracker.get("miss_taxonomy"),
                "frozen_economics": tracker.get("frozen_economics") or {},
                "valid": False,
                "invalid_reason": reason,
                "completed_windows_seconds": sorted(
                    int(value // 1000) for value in tracker.get("completed", ())
                ),
            },
            event_time_ms=int(at_ms),
        )

    def _invalidate_all(self, reason, at_ms):
        for tracking_key, tracker in list(self.pending.items()):
            self._emit_invalid(tracker, reason, at_ms)
            self._close_key(tracking_key)
        self.pending.clear()

    def _observe_futures(self, record):
        previous = record.get("previous_sequence")
        start = record.get("sequence_start")
        if self.last_futures_sequence is not None and (
            (previous is not None and int(previous) != self.last_futures_sequence)
            or (start is not None and int(start) != self.last_futures_sequence + 1)
        ):
            self.last_gap_event_ms = int(record.get("event_time_ms", 0) or 0)
            self._invalidate_all(
                "FUTURES_EXECUTED_FLOW_SEQUENCE_GAP",
                record.get("event_time_ms", 0),
            )
        end = record.get("sequence_end")
        if end is not None:
            self.last_futures_sequence = int(end)

        payload = record.get("payload") or {}
        price = _f(payload.get("last_price"))
        high = _f(payload.get("high"), price)
        low = _f(payload.get("low"), price)
        event_ms = int(
            payload.get("last_event_time_ms")
            or record.get("event_time_ms", 0) or 0
        )
        if price <= 0.0 or event_ms <= 0:
            return
        self.last_futures_event_ms = event_ms
        self.recent_futures.append((event_ms, price, high, low))
        remove = []
        for tracking_key, tracker in tuple(self.pending.items()):
            if event_ms < tracker["start_ms"]:
                continue
            tracker["high"] = max(float(tracker["high"]), high)
            tracker["low"] = min(float(tracker["low"]), low)
            tracker["last_price"] = price
            elapsed = event_ms - int(tracker["start_ms"])
            windows = tracker.get("windows_ms") or WINDOWS_MS
            for window in windows:
                if elapsed < window or window in tracker["completed"]:
                    continue
                direction = 1.0 if tracker["side"] == "LONG" else -1.0
                ref = float(tracker["reference_price"])
                close_bps = direction * (price - ref) / ref * 10000.0
                favorable = (
                    (tracker["high"] - ref) / ref * 10000.0
                    if direction > 0 else (ref - tracker["low"]) / ref * 10000.0
                )
                adverse = (
                    (ref - tracker["low"]) / ref * 10000.0
                    if direction > 0 else (tracker["high"] - ref) / ref * 10000.0
                )
                stop_bps = tracker.get("hard_sl_bps")
                economics = dict(tracker.get("frozen_economics") or {})
                cost_budget = _f(economics.get("cost_budget_bps"), None)
                minimum_net = _f(economics.get("minimum_net_edge_bps"), None)
                economic_threshold = (
                    cost_budget + minimum_net
                    if cost_budget is not None and minimum_net is not None
                    else None
                )
                screen_passed = bool(
                    tracker.get("economic_miss_eligible", False)
                    and economic_threshold is not None
                    and favorable > economic_threshold
                )
                tracker["economic_screen_ever"] = bool(
                    tracker.get("economic_screen_ever", False) or screen_passed
                )
                self.emit(
                    "decision_counterfactual",
                    {
                        "version": VERSION,
                        "cycle_id": tracker.get("cycle_id"),
                        "causal_episode_id": tracker.get("causal_episode_id"),
                        "economic_wave_id": tracker.get("economic_wave_id"),
                        "economic_cluster_id": tracker.get("economic_cluster_id"),
                        "diagnostic_wave_id": tracker.get("diagnostic_wave_id"),
                        "persistent_metaorder_candidate_id": tracker.get(
                            "persistent_metaorder_candidate_id"
                        ),
                        "persistent_candidate_started_at_ms": tracker.get(
                            "persistent_candidate_started_at_ms"
                        ),
                        "persistent_metaorder_evidence": tracker.get(
                            "persistent_metaorder_evidence"
                        ),
                        "sample_scope": tracker.get("sample_scope"),
                        "episode_anchor_rank": tracker.get("anchor_rank"),
                        "anchor_role": tracker.get("anchor_role"),
                        "authority": bool(tracker.get("authority", False)),
                        "economic_miss_eligible": bool(
                            tracker.get("economic_miss_eligible", False)
                        ),
                        "taxonomy_version": tracker.get("taxonomy_version"),
                        "strategy_code_version": tracker.get("strategy_code_version"),
                        "strategy_config_version": tracker.get("strategy_config_version"),
                        "miss_taxonomy": tracker["miss_taxonomy"],
                        "failed_gates": tracker.get("failed_gates", []),
                        "decision": tracker.get("decision"),
                        "decision_reason": tracker.get("reason"),
                        "valid": True,
                        "side": tracker["side"],
                        "reference_price": ref,
                        "window_seconds": window // 1000,
                        "outcome_price": price,
                        "signed_close_bps": round(close_bps, 6),
                        "max_favorable_excursion_bps": round(max(0.0, favorable), 6),
                        "max_adverse_excursion_bps": round(max(0.0, adverse), 6),
                        "frozen_economics": economics,
                        "economic_miss_threshold_bps": (
                            round(economic_threshold, 6)
                            if economic_threshold is not None else None
                        ),
                        "economic_miss_screen_passed": screen_passed,
                        "diagnostic_move_screen_passed": bool(
                            not tracker.get("economic_miss_eligible", False)
                            and economic_threshold is not None
                            and favorable > economic_threshold
                        ),
                        "hypothetical_hard_sl_bps": stop_bps,
                        "hypothetical_hard_sl_hit": bool(
                            stop_bps is not None and adverse >= float(stop_bps)
                        ),
                        "outcome_event_time_ms": event_ms,
                    },
                    event_time_ms=event_ms,
                )
                tracker["completed"].add(window)
                if window == windows[-1]:
                    self._emit_final_adjudication(tracker, event_ms)
            if len(tracker["completed"]) == len(windows):
                remove.append(tracking_key)
        for tracking_key in remove:
            self.pending.pop(tracking_key, None)
            self._close_key(tracking_key)

    def observe(self, record):
        stream = str(record.get("stream", ""))
        if stream == "bot_event":
            self._register(record)
        elif stream == "futures_trade_100ms":
            self._observe_futures(record)
