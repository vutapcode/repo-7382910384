"""Empirical residual-edge accounting for the non-authoritative Wavefront lane.

The live strategy never imports this module.  It deliberately scores the net
outcome produced by the canonical Guardian/Risk path instead of converting a
candle range or an edge label into assumed alpha.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from dataclasses import asdict
import math
import statistics


VERSION = "WAVEFRONT_RESIDUAL_EDGE_V1"
MIN_CANDIDATES = 100
MIN_TRADES = 30
MIN_DAYS = 14.0
MIN_PROFIT_FACTOR = 1.25


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RunningOutcome:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    stress_sum: float = 0.0
    hard_stops: int = 0
    economic_waves: int = 0
    economic_captured: int = 0
    capture_sum: float = 0.0
    unverified_costs: int = 0
    adverse_250_sum: float = 0.0
    adverse_250_count: int = 0
    adverse_1s_sum: float = 0.0
    adverse_1s_count: int = 0

    def add(self, payload):
        value = _f(payload.get("net_pnl_bps"))
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        if value > 0.0:
            self.gross_profit += value
        elif value < 0.0:
            self.gross_loss += abs(value)
        self.stress_sum += _f(payload.get("stress_25bps_net_bps"))
        self.hard_stops += int(str(payload.get("exit_reason", "")).upper() == "HARD_SL")
        economic = bool(payload.get("economic_wave"))
        self.economic_waves += int(economic)
        self.economic_captured += int(economic and value > 0.0)
        self.capture_sum += max(0.0, min(1.0, _f(payload.get("capture_ratio"))))
        self.unverified_costs += int(not bool(payload.get("commission_verified")))
        if payload.get("adverse_selection_250ms_bps") is not None:
            self.adverse_250_sum += min(
                0.0, _f(payload.get("adverse_selection_250ms_bps"))
            )
            self.adverse_250_count += 1
        if payload.get("adverse_selection_1s_bps") is not None:
            self.adverse_1s_sum += min(
                0.0, _f(payload.get("adverse_selection_1s_bps"))
            )
            self.adverse_1s_count += 1

    @property
    def variance(self):
        return self.m2 / (self.count - 1) if self.count > 1 else 0.0

    def snapshot(self):
        pf = (
            self.gross_profit / self.gross_loss
            if self.gross_loss > 0.0 else 999.0 if self.gross_profit > 0.0 else 0.0
        )
        return {
            "samples": self.count,
            "mean_net_bps": round(self.mean, 6),
            "std_net_bps": round(math.sqrt(max(0.0, self.variance)), 6),
            "profit_factor": round(pf, 6),
            "stress_25bps_mean_bps": round(
                self.stress_sum / self.count if self.count else 0.0, 6
            ),
            "hard_stop_rate": round(
                self.hard_stops / self.count if self.count else 0.0, 6
            ),
            "economic_waves": self.economic_waves,
            "runner_recall": round(
                self.economic_captured / self.economic_waves
                if self.economic_waves else 0.0,
                6,
            ),
            "mean_capture_ratio": round(
                self.capture_sum / self.count if self.count else 0.0, 6
            ),
            "unverified_cost_samples": self.unverified_costs,
            "adverse_selection_reserve_250ms_bps": round(
                abs(self.adverse_250_sum / self.adverse_250_count)
                if self.adverse_250_count else 0.0,
                6,
            ),
            "adverse_selection_reserve_1s_bps": round(
                abs(self.adverse_1s_sum / self.adverse_1s_count)
                if self.adverse_1s_count else 0.0,
                6,
            ),
        }


class ResidualEdgeBook:
    """Bounded, deterministic cohort statistics with hierarchical shrinkage."""

    def __init__(self):
        self.stats = defaultdict(RunningOutcome)
        self.candidates = 0
        self.first_candidate_ms = None
        self.last_candidate_ms = None
        self.core_entries = 0
        self.core_exits = 0
        self.core_hard_stops = 0
        self.core_capture_sum = 0.0
        self.core_entry_events = deque(maxlen=512)
        self.entry_advances_ms = deque(maxlen=2048)
        self.wavefront_economic_missed_by_core = 0
        self.wavefront_economic_recovered = 0

    @staticmethod
    def exact_key(payload):
        return (
            str(payload.get("causal_class") or "UNKNOWN"),
            str(payload.get("side") or "UNKNOWN"),
            str(payload.get("proposer") or "UNKNOWN"),
            str(payload.get("bias_relation") or "UNKNOWN"),
            str(payload.get("regime") or "UNKNOWN"),
            str(payload.get("execution_twin") or "UNKNOWN"),
        )

    @staticmethod
    def broad_key(payload):
        return (
            str(payload.get("causal_class") or "UNKNOWN"),
            str(payload.get("side") or "UNKNOWN"),
            "*", "*", "*",
            str(payload.get("execution_twin") or "UNKNOWN"),
        )

    @staticmethod
    def global_key(payload):
        return ("*", "*", "*", "*", "*", str(payload.get("execution_twin") or "UNKNOWN"))

    def observe_candidate(self, event_ms):
        event_ms = int(event_ms or 0)
        self.candidates += 1
        if self.first_candidate_ms is None:
            self.first_candidate_ms = event_ms
        self.last_candidate_ms = event_ms

    def observe_core_event(self, event, payload, event_ms):
        event = str(event or "").upper()
        payload = payload or {}
        if event == "ENTRY":
            self.core_entries += 1
            self.core_entry_events.append({
                "event_ms": int(event_ms or 0),
                "side": str(payload.get("side") or "").upper(),
            })
        elif event == "EXIT":
            self.core_exits += 1
            reason = str(
                payload.get("risk_reason")
                or (payload.get("guardian_state") or {}).get("reason") or ""
            ).upper()
            self.core_hard_stops += int(reason == "HARD_SL")
            best_r = max(0.0, _f(payload.get("best_r")))
            net_r = _f(payload.get("net_pnl_r"))
            self.core_capture_sum += (
                max(0.0, min(1.0, net_r / best_r)) if best_r > 0.0 else 0.0
            )

    def match_core_entry(self, side, wavefront_fill_ms, exit_ms):
        side = str(side or "").upper()
        matches = [
            row for row in self.core_entry_events
            if row["side"] == side
            and int(wavefront_fill_ms) <= row["event_ms"] <= int(exit_ms)
        ]
        if not matches:
            return None
        core_ms = min(row["event_ms"] for row in matches)
        advance = core_ms - int(wavefront_fill_ms)
        self.entry_advances_ms.append(advance)
        return advance

    def observe_exit(self, payload):
        if not payload.get("valid") or not payload.get("filled"):
            return self.report(payload)
        keys = (
            self.exact_key(payload), self.broad_key(payload), self.global_key(payload)
        )
        for key in keys:
            self.stats[key].add(payload)

        if payload.get("economic_wave") and not payload.get("core_shared_entry"):
            self.wavefront_economic_missed_by_core += 1
            if _f(payload.get("net_pnl_bps")) > 0.0:
                self.wavefront_economic_recovered += 1
        return self.report(payload)

    def _shrunk(self, payload):
        exact = self.stats[self.exact_key(payload)]
        broad = self.stats[self.broad_key(payload)]
        global_row = self.stats[self.global_key(payload)]
        parent = broad if broad.count else global_row
        weight = exact.count / (exact.count + 10.0) if exact.count else 0.0
        mean = weight * exact.mean + (1.0 - weight) * parent.mean
        variance = (
            weight * exact.variance + (1.0 - weight) * parent.variance
        )
        effective_n = max(1.0, exact.count + min(10.0, parent.count) * (1.0 - weight))
        lcb = mean - 1.96 * math.sqrt(max(0.0, variance) / effective_n)
        return mean, lcb, weight, parent.count

    def report(self, payload=None):
        payload = payload or {"execution_twin": "TAKER_TWIN"}
        exact = self.stats[self.exact_key(payload)]
        mean, lcb, weight, parent_n = self._shrunk(payload)
        snap = exact.snapshot()
        days = (
            max(0.0, (self.last_candidate_ms - self.first_candidate_ms) / 86_400_000.0)
            if self.first_candidate_ms is not None and self.last_candidate_ms is not None
            else 0.0
        )
        core_hard_rate = self.core_hard_stops / self.core_exits if self.core_exits else 0.0
        miss_reduction = (
            self.wavefront_economic_recovered / self.wavefront_economic_missed_by_core
            if self.wavefront_economic_missed_by_core else 0.0
        )
        median_advance = (
            statistics.median(self.entry_advances_ms) if self.entry_advances_ms else None
        )
        blockers = []
        if days < MIN_DAYS:
            blockers.append("MIN_14_DAYS")
        if self.candidates < MIN_CANDIDATES:
            blockers.append("MIN_100_CANDIDATES")
        if snap["samples"] < MIN_TRADES:
            blockers.append("MIN_30_VIRTUAL_TRADES")
        if snap["profit_factor"] < MIN_PROFIT_FACTOR:
            blockers.append("PF_BELOW_1_25")
        if mean <= 0.0:
            blockers.append("EXPECTANCY_NOT_POSITIVE")
        if lcb < 0.0:
            blockers.append("LCB_NEGATIVE")
        if snap["stress_25bps_mean_bps"] < 0.0:
            blockers.append("STRESS_25BPS_NEGATIVE")
        if snap["unverified_cost_samples"]:
            blockers.append("UNVERIFIED_COST_SAMPLES")
        if miss_reduction < 0.25:
            blockers.append("MISS_REDUCTION_BELOW_25PCT")
        if median_advance is None or median_advance < 500.0:
            blockers.append("ENTRY_ADVANCE_BELOW_500MS")
        if snap["hard_stop_rate"] > core_hard_rate + 0.05 and self.core_exits:
            blockers.append("HARD_STOP_RATE_REGRESSION")
        core_capture = self.core_capture_sum / self.core_exits if self.core_exits else None
        if core_capture is not None and snap["mean_capture_ratio"] < core_capture:
            blockers.append("CAPTURE_RATIO_REGRESSION")

        return {
            "version": VERSION,
            "authority": False,
            "manual_approval_required": True,
            "promotion_eligible": False,
            "promotion_evidence_complete": not blockers,
            "promotion_blockers": blockers + ["MANUAL_APPROVAL_REQUIRED"],
            "causal_class": self.exact_key(payload)[0],
            "side": self.exact_key(payload)[1],
            "execution_twin": self.exact_key(payload)[5],
            "candidate_count": self.candidates,
            "collection_days": round(days, 6),
            **snap,
            "residual_edge_mean_bps": round(mean, 6),
            "residual_edge_lcb_bps": round(lcb, 6),
            "hierarchical_exact_weight": round(weight, 6),
            "hierarchical_parent_samples": parent_n,
            "miss_reduction_ratio": round(miss_reduction, 6),
            "median_entry_advance_ms": median_advance,
            "core_hard_stop_rate": round(core_hard_rate, 6),
            "core_mean_capture_ratio": (
                round(core_capture, 6) if core_capture is not None else None
            ),
            "policy": "EMPIRICAL_GUARDIAN_OUTCOME_NOT_HEURISTIC_RANGE",
        }

    def to_state(self):
        return {
            "version": VERSION,
            "candidates": self.candidates,
            "first_candidate_ms": self.first_candidate_ms,
            "last_candidate_ms": self.last_candidate_ms,
            "core_entries": self.core_entries,
            "core_exits": self.core_exits,
            "core_hard_stops": self.core_hard_stops,
            "core_capture_sum": self.core_capture_sum,
            "core_entry_events": list(self.core_entry_events),
            "entry_advances_ms": list(self.entry_advances_ms),
            "wavefront_economic_missed_by_core": self.wavefront_economic_missed_by_core,
            "wavefront_economic_recovered": self.wavefront_economic_recovered,
            "stats": [
                {"key": list(key), "outcome": asdict(value)}
                for key, value in sorted(self.stats.items(), key=lambda item: item[0])
                if value.count
            ],
        }

    @classmethod
    def from_state(cls, payload):
        book = cls()
        if not isinstance(payload, dict) or payload.get("version") != VERSION:
            return book
        book.candidates = int(payload.get("candidates", 0) or 0)
        book.first_candidate_ms = payload.get("first_candidate_ms")
        book.last_candidate_ms = payload.get("last_candidate_ms")
        book.core_entries = int(payload.get("core_entries", 0) or 0)
        book.core_exits = int(payload.get("core_exits", 0) or 0)
        book.core_hard_stops = int(payload.get("core_hard_stops", 0) or 0)
        book.core_capture_sum = _f(payload.get("core_capture_sum"))
        book.core_entry_events.extend(payload.get("core_entry_events") or ())
        book.entry_advances_ms.extend(payload.get("entry_advances_ms") or ())
        book.wavefront_economic_missed_by_core = int(
            payload.get("wavefront_economic_missed_by_core", 0) or 0
        )
        book.wavefront_economic_recovered = int(
            payload.get("wavefront_economic_recovered", 0) or 0
        )
        fields = RunningOutcome.__dataclass_fields__
        for row in payload.get("stats") or ():
            key = tuple(row.get("key") or ())
            outcome = row.get("outcome") or {}
            if len(key) != 6:
                continue
            clean = {name: outcome[name] for name in fields if name in outcome}
            book.stats[key] = RunningOutcome(**clean)
        return book
