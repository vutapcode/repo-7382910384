"""Bounded counterfactual outcomes for recorded Tier-S misses.

This is recorder-only evidence.  It never feeds the live strategy. Outcomes use
the first causally received Futures aggTrade batch at/after each horizon and are
invalidated on a sequence discontinuity instead of bridging a data gap.
"""

from collections import deque, OrderedDict


WINDOWS_MS = (5_000, 15_000, 30_000, 60_000)
MAX_PENDING = 512
MAX_CLOSED = 2_048
VERSION = "DECISION_COUNTERFACTUAL_V4_DUAL_ANCHOR"


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
            if qualified_now:
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
                    decision.get("causal_episode_id")
                    or payload.get("causal_episode_id")
                ),
                "miss_taxonomy": output.get("miss_taxonomy") or payload.get("miss_taxonomy"),
                "failed_gates": output.get("failed_gates") or payload.get("failed_gates") or [],
                "counterfactual": counterfactual,
                "decision": output.get("decision") or payload.get("decision"),
                "reason": output.get("reason") or payload.get("reason"),
                "taxonomy_version": decision.get("taxonomy_version"),
                "strategy_code_version": decision.get("strategy_code_version"),
                "strategy_config_version": decision.get("strategy_config_version"),
                "anchor_rank": rank,
                "anchor_role": anchor_role,
                "qualified_now": qualified_now,
            }
        if event in ("ENTRY_SKIPPED", "SHADOW_MAKER_CANCELED"):
            return {
                "cycle_id": payload.get("cycle_id"),
                "causal_episode_id": payload.get("causal_episode_id"),
                "miss_taxonomy": payload.get("miss_taxonomy"),
                "failed_gates": payload.get("failed_gates") or [
                    payload.get("miss_taxonomy")
                ],
                "counterfactual": payload.get("counterfactual") or {},
                "decision": "SKIP",
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

    def _register(self, record):
        candidate = self._candidate(record)
        if not candidate:
            return
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
        base_key = episode_id or cycle_id
        anchor_role = str(candidate.get("anchor_role") or "EPISODE_ORIGIN")
        suffix = (
            "::EXECUTION" if episode_id and anchor_role == "EXECUTION"
            else "::GO" if episode_id and anchor_role in ("QUALIFIED", "GO_CANDIDATE")
            else ""
        )
        tracking_key = base_key + suffix
        if tracking_key in self.closed:
            return
        existing = self.pending.get(tracking_key)
        if existing is not None:
            better_anchor = int(candidate.get("anchor_rank", 1) or 1) > int(
                existing.get("anchor_rank", 1) or 1
            )
            if not better_anchor or existing.get("completed"):
                return
            self.pending.pop(tracking_key, None)
        self.pending[tracking_key] = {
            **candidate,
            "tracking_key": tracking_key,
            "sample_scope": (
                "CAUSAL_EPISODE_EXECUTION"
                if suffix == "::EXECUTION"
                else "CAUSAL_EPISODE_GO_ANCHOR"
                if suffix == "::GO"
                else "CAUSAL_EPISODE_ORIGIN"
                if episode_id
                else "DECISION_CYCLE"
            ),
            "start_ms": start_ms,
            "side": side,
            "reference_price": reference,
            "hard_sl_bps": _f(cf.get("hard_sl_bps"), None),
            "high": reference,
            "low": reference,
            "last_price": reference,
            "completed": set(),
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
                "sample_scope": tracker.get("sample_scope"),
                "episode_anchor_rank": tracker.get("anchor_rank"),
                "anchor_role": tracker.get("anchor_role"),
                "taxonomy_version": tracker.get("taxonomy_version"),
                "strategy_code_version": tracker.get("strategy_code_version"),
                "strategy_config_version": tracker.get("strategy_config_version"),
                "miss_taxonomy": tracker.get("miss_taxonomy"),
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
            for window in WINDOWS_MS:
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
                self.emit(
                    "decision_counterfactual",
                    {
                        "version": VERSION,
                        "cycle_id": tracker.get("cycle_id"),
                        "causal_episode_id": tracker.get("causal_episode_id"),
                        "sample_scope": tracker.get("sample_scope"),
                        "episode_anchor_rank": tracker.get("anchor_rank"),
                        "anchor_role": tracker.get("anchor_role"),
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
                        "hypothetical_hard_sl_bps": stop_bps,
                        "hypothetical_hard_sl_hit": bool(
                            stop_bps is not None and adverse >= float(stop_bps)
                        ),
                        "outcome_event_time_ms": event_ms,
                    },
                    event_time_ms=event_ms,
                )
                tracker["completed"].add(window)
            if len(tracker["completed"]) == len(WINDOWS_MS):
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
