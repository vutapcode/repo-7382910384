"""Authority-free event/state/hypothesis research brain.

This module deliberately has no order, Entry, Guardian or Risk imports.  It
turns recorder evidence into falsifiable mechanism hypotheses so the current
canonical strategy can be compared against a coherent shadow explanation.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import hashlib
import json


VERSION = "CAUSAL_EPISODE_GRAPH_V1_SHADOW"
OUTPUT_STREAM = "causal_world_state"
CASH_STREAMS = {
    "binance_spot_trade_100ms": "binance_spot",
    "coinbase_spot_trade_100ms": "coinbase_spot",
}
LIQUIDITY_STREAMS = {
    "spot_liquidity_response": "binance_spot",
    "coinbase_liquidity_response": "coinbase_spot",
    "liquidity_response": "futures",
}
QUESTION_CONTRACTS = {
    "CASH_METAORDER": {
        "question": "DOES_INDEPENDENT_CASH_FLOW_CONTROL_PRICE",
        "confirm": "DUAL_CASH_EXECUTED_FLOW_CONVERTS",
        "refute": "CASH_FLOW_STOPS_CONVERTING_OR_OPPOSITE_CASH_ACCEPTS",
    },
    "FORCED_UNWIND": {
        "question": "IS_DERIVATIVE_FLOW_FORCED_CLOSING_NOT_NEW_CONTROL",
        "confirm": "OI_CONTRACTS_WITH_LIQUIDATION_AND_FUTURES_AGGRESSION",
        "refute": "OI_BUILDS_OR_CASH_ASSUMES_SAME_SIDE_CONTROL",
    },
    "DERIVATIVE_DISLOCATION": {
        "question": "ARE_DERIVATIVES_MOVING_WITHOUT_CASH_CONTROL",
        "confirm": "FUTURES_AGGRESSION_WITHOUT_DUAL_CASH_ACCEPTANCE",
        "refute": "DUAL_CASH_ACCEPTS_OR_FUTURES_FLOW_DECAYS",
    },
    "LIQUIDITY_VACUUM": {
        "question": "IS_PRICE_MOVING_THROUGH_WITHDRAWN_THIN_LIQUIDITY",
        "confirm": "WITHDRAWAL_OR_DEPLETION_WITH_LOW_EXECUTED_FLOW_CONVERSION",
        "refute": "REFILL_OR_ABSORPTION_RESTORES_QUEUE",
    },
    "ABSORPTION_CONTROL_TRANSFER": {
        "question": "DID_OLD_SIDE_FAIL_AND_OPPOSITE_CASH_TAKE_CONTROL",
        "confirm": "ABSORPTION_THEN_OPPOSITE_DUAL_CASH_ACCEPTANCE",
        "refute": "OLD_SIDE_RESUMES_PRICE_CONVERSION",
    },
}


def _f(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class MarketEventV1:
    stream: str
    source: str
    event_time_ms: int
    receive_time_ms: int
    available_time_ms: int
    epoch: int
    sequence_start: int | None
    sequence_end: int | None
    clock_valid: bool
    source_health: str = "UNKNOWN"
    temporal_uncertainty_ms: float = 0.0

    @classmethod
    def from_record(cls, record):
        payload = dict(record.get("payload") or {})
        receive = int(record.get("receive_time_ms", 0) or 0)
        event = int(
            record.get("exchange_event_time_ms")
            or record.get("event_time_ms")
            or receive
        )
        available = int(
            record.get("available_time_ms")
            or payload.get("batch_available_time_ms")
            or receive
        )
        health = str(record.get("source_health") or "UNKNOWN").upper()
        # Availability, never corrected exchange time, is the no-lookahead
        # boundary. Availability may follow socket receipt because parsing and
        # micro-batch finalization take time; it may never precede receipt.
        valid = bool(
            receive > 0 and available >= receive
            and event <= receive + 1_000
            and payload.get("clock_valid", True)
            and health not in {"STALE", "DEAD", "CONTRADICTORY"}
        )
        return cls(
            stream=str(record.get("stream") or ""),
            source=str(record.get("source") or "unknown"),
            event_time_ms=event,
            receive_time_ms=receive,
            available_time_ms=available,
            epoch=int(record.get("epoch", payload.get("epoch", 0)) or 0),
            sequence_start=record.get("sequence_start"),
            sequence_end=record.get("sequence_end"),
            clock_valid=valid,
            source_health=health,
            temporal_uncertainty_ms=float(
                record.get("temporal_uncertainty_ms", 0.0) or 0.0
            ),
        )


class CausalWorldModel:
    """Small deterministic competing-hypothesis graph, always authority=false."""

    def __init__(self, emit=None, horizon_ms=5_000):
        self.emit = emit
        self.horizon_ms = max(1_000, int(horizon_ms))
        self.cash = {name: deque(maxlen=64) for name in ("binance_spot", "coinbase_spot")}
        self.futures = deque(maxlen=64)
        self.liquidity = {}
        self.last_oi = None
        self.oi_state = "UNKNOWN"
        self.liquidation = deque(maxlen=32)
        self.source_epoch = {}
        self.episode_id = None
        self.episode_side = "ABSTAIN"
        self.episode_started_ms = 0
        self.revision = 0
        self.last_hash = None
        self.last_emit_ms = 0
        self.emitted = 0
        self.invalid = 0

    @staticmethod
    def _trade_observation(payload, at_ms):
        buy, sell = _f(payload.get("buy_qty")), _f(payload.get("sell_qty"))
        total = buy + sell
        if total <= 0.0:
            return None
        imbalance = (buy - sell) / total
        if abs(imbalance) < 0.20:
            side = "NEUTRAL"
        else:
            side = "LONG" if imbalance > 0.0 else "SHORT"
        first, last = _f(payload.get("first_price")), _f(payload.get("last_price"))
        progress = ((last - first) / first * 10_000.0) if first > 0.0 and last > 0.0 else 0.0
        signed_progress = progress * (1.0 if side == "LONG" else -1.0)
        return {
            "at_ms": int(at_ms), "side": side,
            "imbalance": round(imbalance, 6),
            "quote": round(_f(payload.get("buy_quote")) + _f(payload.get("sell_quote")), 6),
            "converts": bool(side in ("LONG", "SHORT") and signed_progress > 0.0),
            "signed_progress_bps": round(signed_progress, 6),
            "evidence_id": "%s:%s:%s" % (
                payload.get("first_trade_id"), payload.get("last_trade_id"), at_ms
            ),
        }

    def _trim(self, now_ms):
        cutoff = int(now_ms) - self.horizon_ms
        for rows in tuple(self.cash.values()) + (self.futures, self.liquidation):
            while rows and int(rows[0].get("at_ms", 0)) < cutoff:
                rows.popleft()

    def _dual_cash(self, now_ms):
        latest = {}
        for venue, rows in self.cash.items():
            for row in reversed(rows):
                if now_ms - row["at_ms"] <= 600 and row["side"] in ("LONG", "SHORT") and row["converts"]:
                    latest[venue] = row
                    break
        if len(latest) != 2:
            return None, []
        sides = {row["side"] for row in latest.values()}
        if len(sides) != 1:
            return None, list(latest.values())
        return sides.pop(), list(latest.values())

    def _hypotheses(self, now_ms):
        dual_side, cash_rows = self._dual_cash(now_ms)
        latest_futures = self.futures[-1] if self.futures else None
        latest_liq = self.liquidation[-1] if self.liquidation else None
        liquidity_rows = [
            row for row in self.liquidity.values()
            if now_ms - int(row.get("at_ms", 0)) <= 3_000
        ]
        absorption = next((row for row in liquidity_rows if row.get("absorption")), None)
        vacuum = next((row for row in liquidity_rows if row.get("vacuum")), None)
        out = {name: {"state": "UNKNOWN", "evidence": [], "falsifiers": []}
               for name in QUESTION_CONTRACTS}

        if dual_side:
            out["CASH_METAORDER"] = {
                "state": "SUPPORTED", "side": dual_side,
                "evidence": [row["evidence_id"] for row in cash_rows],
                "falsifiers": ["OPPOSITE_CASH_ACCEPTANCE", "FLOW_NONCONVERSION"],
            }
        if latest_futures and latest_futures["side"] in ("LONG", "SHORT") and not dual_side:
            out["DERIVATIVE_DISLOCATION"] = {
                "state": "SUPPORTED", "side": latest_futures["side"],
                "evidence": [latest_futures["evidence_id"]],
                "falsifiers": ["DUAL_CASH_ACCEPTANCE", "FUTURES_FLOW_DECAY"],
            }
        if self.oi_state == "CONTRACTION" and latest_liq and latest_futures:
            if latest_liq.get("side") == latest_futures.get("side"):
                out["FORCED_UNWIND"] = {
                    "state": "SUPPORTED", "side": latest_liq["side"],
                    "evidence": [latest_liq["evidence_id"], latest_futures["evidence_id"]],
                    "falsifiers": ["OI_BUILD", "CASH_CONTROL_PERSISTS_AFTER_LIQUIDATION"],
                }
        if vacuum:
            out["LIQUIDITY_VACUUM"] = {
                "state": "SUPPORTED", "side": vacuum.get("side", "ABSTAIN"),
                "evidence": [vacuum.get("evidence_id")],
                "falsifiers": ["QUEUE_REFILL", "ABSORPTION"],
            }
        if absorption and dual_side and absorption.get("side") not in (None, dual_side):
            out["ABSORPTION_CONTROL_TRANSFER"] = {
                "state": "SUPPORTED", "side": dual_side,
                "evidence": [absorption.get("evidence_id")]
                    + [row["evidence_id"] for row in cash_rows],
                "falsifiers": ["OLD_SIDE_CONVERSION_RESUMES"],
            }
            out["CASH_METAORDER"]["state"] = "SUPPORTED"
        return out, dual_side

    def _emit_state(self, now_ms, reason):
        hypotheses, dual_side = self._hypotheses(now_ms)
        supported = [name for name, row in hypotheses.items() if row["state"] == "SUPPORTED"]
        side = dual_side or (
            hypotheses["ABSORPTION_CONTROL_TRANSFER"].get("side")
            if hypotheses["ABSORPTION_CONTROL_TRANSFER"]["state"] == "SUPPORTED"
            else "ABSTAIN"
        )
        if side in ("LONG", "SHORT") and (
            self.episode_id is None or self.episode_side != side
            or now_ms - self.episode_started_ms > self.horizon_ms
        ):
            self.episode_started_ms = int(now_ms)
            self.episode_side = side
            self.episode_id = "world:%s:%s" % (side, self.episode_started_ms)
        semantic = {
            "episode": self.episode_id,
            "side": self.episode_side,
            "hypotheses": {
                name: (row.get("state"), row.get("side", "ABSTAIN"))
                for name, row in hypotheses.items()
            },
            "oi_state": self.oi_state,
            "missing_dual_cash": not bool(dual_side),
        }
        digest = hashlib.sha256(json.dumps(
            semantic, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        if digest == self.last_hash and now_ms - self.last_emit_ms < 1_000:
            return None
        self.revision += 1
        payload = {
            "version": VERSION,
            "authority": False,
            "eligible_for_entry": False,
            "causal_episode_id": self.episode_id,
            "episode_side": self.episode_side,
            "episode_started_ms": self.episode_started_ms or None,
            "revision": self.revision,
            "reason": reason,
            "hypotheses": hypotheses,
            "supported_hypotheses": supported,
            "question_contract_version": VERSION,
            "oi_state": self.oi_state,
            "action_readiness": {
                "state": "RESEARCH_ONLY",
                "missing_evidence": (
                    [] if dual_side else ["DUAL_CASH_EXECUTED_FLOW_CONVERSION"]
                ),
                "policy": "NO_LIVE_OR_SHADOW_ENTRY_AUTHORITY",
            },
        }
        payload["state_hash"] = digest
        self.last_hash = digest
        self.last_emit_ms = int(now_ms)
        self.emitted += 1
        if self.emit is not None:
            self.emit(OUTPUT_STREAM, payload, event_time_ms=int(now_ms))
        return payload

    def observe(self, record):
        event = MarketEventV1.from_record(record)
        if event.stream == OUTPUT_STREAM:
            return None
        if not event.clock_valid:
            self.invalid += 1
            return None
        payload = dict(record.get("payload") or {})
        now_ms = event.available_time_ms
        self._trim(now_ms)
        epoch_key = (event.source, event.stream)
        previous_epoch = self.source_epoch.get(epoch_key)
        if event.epoch and previous_epoch not in (None, event.epoch):
            self.cash = {name: deque(maxlen=64) for name in self.cash}
            self.futures.clear(); self.liquidation.clear(); self.liquidity.clear()
            self.episode_id = None; self.episode_side = "ABSTAIN"
        if event.epoch:
            self.source_epoch[epoch_key] = event.epoch

        changed = False
        if event.stream in CASH_STREAMS:
            row = self._trade_observation(payload, now_ms)
            if row:
                self.cash[CASH_STREAMS[event.stream]].append(row); changed = True
        elif event.stream == "futures_trade_100ms":
            row = self._trade_observation(payload, now_ms)
            if row:
                self.futures.append(row); changed = True
        elif event.stream == "open_interest":
            oi = _f(payload.get("openInterest"))
            if oi > 0.0:
                if self.last_oi is None:
                    self.oi_state = "UNKNOWN"
                elif oi > self.last_oi:
                    self.oi_state = "BUILD"
                elif oi < self.last_oi:
                    self.oi_state = "CONTRACTION"
                else:
                    self.oi_state = "UNCHANGED_UNKNOWN"
                self.last_oi = oi; changed = True
        elif event.stream == "liquidation":
            order = dict(payload.get("o") or payload)
            exchange_side = str(order.get("S") or "").upper()
            side = "SHORT" if exchange_side == "SELL" else "LONG" if exchange_side == "BUY" else "ABSTAIN"
            self.liquidation.append({
                "at_ms": now_ms, "side": side,
                "evidence_id": "liquidation:%s:%s" % (side, now_ms),
            }); changed = True
        elif event.stream in LIQUIDITY_STREAMS:
            absorption = bool(payload.get("absorption_candidate"))
            responses = dict(payload.get("responses") or {})
            last_response = responses.get("500") or responses.get("1000") or {}
            vacuum = bool(
                payload.get("liquidity_vacuum_candidate")
                or (
                    _f(payload.get("executed_depletion_ratio")) > 0.5
                    and _f(payload.get("signed_price_move_bps")) > 0.5
                    and not absorption
                )
                or (
                    _f(last_response.get("opposite_queue_change_qty")) < 0.0
                    and _f(last_response.get("signed_mid_response_bps")) > 0.5
                )
            )
            side = str(payload.get("side") or "ABSTAIN").upper()
            self.liquidity[LIQUIDITY_STREAMS[event.stream]] = {
                "at_ms": now_ms, "side": side, "absorption": absorption,
                "vacuum": vacuum,
                "evidence_id": "%s:%s" % (event.stream, now_ms),
            }; changed = True
        return self._emit_state(now_ms, event.stream) if changed else None

    def summary(self):
        return {
            "version": VERSION, "authority": False,
            "emitted": self.emitted, "invalid": self.invalid,
            "active_episode_id": self.episode_id,
            "active_side": self.episode_side,
            "last_state_hash": self.last_hash,
        }
