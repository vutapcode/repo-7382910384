"""Parallel, recorder-only Wavefront Entry and virtual execution twins.

This module has no order API and is intentionally absent from the canonical bot
launcher.  It consumes records in receive order, uses exchange event time only
to test causal ordering, and emits append-only research records with
``authority=false``.
"""

from __future__ import annotations

from collections import deque
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

from loi_he_thong import risk_ratchet_price_quality_hook
from loi_he_thong import shadow_risk_guard
from loi_he_thong import ignition_core, ignition_signals, verified_cost_model
from recorder.residual_edge import ResidualEdgeBook


VERSION = "WAVEFRONT_SHADOW_V3_CANONICAL_MIRROR"
AUTHORITY = False
FOLLOW_MAX_MS = 1_200
LEAD_FLOOR_MS = 100.0
MAKER_TTL_MS = 750
QTY_BTC = 0.001
STOP_FRACTION = 0.0055
GENERATED_STREAMS = frozenset({
    "wavefront_candidate", "wavefront_virtual_entry",
    "wavefront_virtual_exit", "residual_edge_report",
    "liquidity_response",
})
TRADE_STREAMS = {
    "binance_spot_trade_100ms": "binance_spot",
    "coinbase_spot_trade_100ms": "coinbase_spot",
    "futures_trade_100ms": "futures",
}
CASH_VENUES = frozenset({"binance_spot", "coinbase_spot"})
MIN_QTY = {"binance_spot": 0.02, "coinbase_spot": 0.01, "futures": 0.02}
CANONICAL_MIRROR_PROFILE = "CANONICAL_MIRROR"
CANONICAL_MIRROR_VERSION = "CANONICAL_MIRROR_V1"


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _side_sign(side):
    return 1.0 if str(side).upper() == "LONG" else -1.0


def _mid(bid, ask):
    bid, ask = _f(bid), _f(ask)
    return (bid + ask) / 2.0 if bid > 0.0 and ask > bid else 0.0


class ClockQuality:
    """O(1) lower-envelope clock mapping plus bounded jitter uncertainty."""

    __slots__ = (
        "samples", "base_delay_ms", "jitter_ms", "last_corrected_ms",
        "last_valid",
    )

    def __init__(self):
        self.samples = 0
        self.base_delay_ms = 0.0
        self.jitter_ms = 0.0
        self.last_corrected_ms = 0.0
        self.last_valid = True

    def observe(self, event_ms, receive_ms):
        event_ms, receive_ms = float(event_ms), float(receive_ms)
        delay = receive_ms - event_ms
        if self.samples == 0:
            self.base_delay_ms = delay
            self.jitter_ms = 5.0
        else:
            # A slowly rising minimum follows clock drift without mistaking a
            # latency spike for exchange causality.
            self.base_delay_ms = min(delay, self.base_delay_ms + 0.05)
            residual = abs(delay - self.base_delay_ms)
            self.jitter_ms += 0.05 * (residual - self.jitter_ms)
        self.samples += 1
        corrected = event_ms + self.base_delay_ms
        self.last_valid = bool(
            corrected + 50.0 >= self.last_corrected_ms
            and event_ms <= receive_ms + 1_000.0
        )
        self.last_corrected_ms = max(self.last_corrected_ms, corrected)
        return self.last_corrected_ms

    @property
    def uncertainty_ms(self):
        if self.samples < 5:
            return 250.0
        return max(5.0, min(250.0, 3.0 * self.jitter_ms))

    def snapshot(self):
        return {
            "samples": self.samples,
            "base_delay_ms": round(self.base_delay_ms, 4),
            "uncertainty_ms": round(self.uncertainty_ms, 4),
            "valid": self.last_valid,
        }


class FlowBaseline:
    """Streaming surprise detector; current impulse is scored before learning it."""

    __slots__ = ("samples", "mean_abs_quote", "mean_dev", "last_price")

    def __init__(self):
        self.samples = 0
        self.mean_abs_quote = 0.0
        self.mean_dev = 0.0
        self.last_price = 0.0

    def score(
        self, payload, venue, warmup_samples, *, min_qty=None,
        material_price_bps=0.15,
    ):
        buy_qty = _f(payload.get("buy_qty"))
        sell_qty = _f(payload.get("sell_qty"))
        buy_quote = _f(payload.get("buy_quote"))
        sell_quote = _f(payload.get("sell_quote"))
        total_qty = buy_qty + sell_qty
        signed_quote = buy_quote - sell_quote
        abs_quote = abs(signed_quote)
        imbalance = (buy_qty - sell_qty) / total_qty if total_qty > 0.0 else 0.0
        price = _f(payload.get("last_price"))
        price_bps = (
            (price - self.last_price) / self.last_price * 10_000.0
            if price > 0.0 and self.last_price > 0.0 else 0.0
        )
        threshold = self.mean_abs_quote + 2.0 * max(self.mean_dev, self.mean_abs_quote * 0.10)
        surprise_ratio = abs_quote / max(1.0, self.mean_abs_quote + self.mean_dev)
        side = "LONG" if signed_quote > 0.0 else "SHORT" if signed_quote < 0.0 else "NEUTRAL"
        sign = _side_sign(side) if side in ("LONG", "SHORT") else 0.0
        strong = bool(
            self.samples >= int(warmup_samples)
            and total_qty >= float((min_qty or MIN_QTY)[venue])
            and abs(imbalance) >= 0.20
            and abs_quote >= threshold
            and surprise_ratio >= 1.50
            and sign * price_bps >= float(material_price_bps)
        )
        result = {
            "venue": venue, "side": side, "strong": strong,
            "buy_qty": buy_qty, "sell_qty": sell_qty,
            "total_qty": total_qty, "imbalance": round(imbalance, 6),
            "signed_quote": signed_quote, "price": price,
            "high": _f(payload.get("high"), price),
            "low": _f(payload.get("low"), price),
            "price_conversion_bps": round(price_bps, 6),
            "surprise_ratio": round(surprise_ratio, 6),
            "baseline_samples": self.samples,
            "baseline_abs_quote": round(self.mean_abs_quote, 6),
        }
        if self.samples == 0:
            self.mean_abs_quote = abs_quote
            self.mean_dev = abs_quote * 0.25
        else:
            deviation = abs(abs_quote - self.mean_abs_quote)
            self.mean_abs_quote += 0.02 * (abs_quote - self.mean_abs_quote)
            self.mean_dev += 0.02 * (deviation - self.mean_dev)
        self.samples += 1
        if price > 0.0:
            self.last_price = price
        return result


def _load_guardian():
    path = Path(__file__).resolve().parents[1] / "3_thuc_thi" / "ve_si_lenh" / "guardian_s_tier.py"
    spec = importlib.util.spec_from_file_location("wstrade_wavefront_guardian_v6", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("GUARDIAN_V6_LOAD_FAILED")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WavefrontShadowEvaluator:
    """Receive-order evaluator with no live trading authority."""

    def __init__(
        self, emit, *, warmup_samples=20, runtime_health_path=None,
        cpu_status_path=None, feed_health=None, state_path=None,
        evidence_version=None, profile="WAVEFRONT", ablation=None,
    ):
        self.emit = emit
        self.warmup_samples = max(2, int(warmup_samples))
        self.runtime_health_path = Path(runtime_health_path) if runtime_health_path else None
        self.cpu_status_path = Path(cpu_status_path) if cpu_status_path else None
        self.feed_health = feed_health
        self.state_path = Path(state_path) if state_path else None
        self.evidence_version = str(evidence_version or VERSION)
        self.profile = str(profile or "WAVEFRONT").upper()
        ablation = dict(ablation or {})
        if len(ablation) > 1:
            raise ValueError("CANONICAL_MIRROR_ABLATION_MUST_CHANGE_ONE_RULE")
        unknown = set(ablation).difference({
            "follow_max_ms", "material_price_bps",
            "dual_cash_futures_optional",
        })
        if unknown:
            raise ValueError(
                "CANONICAL_MIRROR_ABLATION_UNKNOWN:" + ",".join(sorted(unknown))
            )
        self.ablation = ablation
        if self.profile == CANONICAL_MIRROR_PROFILE:
            self.follow_max_ms = int(ignition_core.FOLLOW_MAX_MS)
            self.bucket_ms = int(ignition_signals.BUCKET_MS)
            self.min_qty = dict(ignition_signals.MIN_QTY)
            self.material_price_bps = float(ignition_core.MATERIAL_PRICE_BPS)
            self.dual_cash_futures_optional = bool(
                ablation.get("dual_cash_futures_optional", False)
            )
            if "follow_max_ms" in ablation:
                self.follow_max_ms = int(ablation["follow_max_ms"])
            elif "material_price_bps" in ablation:
                self.material_price_bps = float(ablation["material_price_bps"])
        else:
            if ablation:
                raise ValueError("ABLATION_REQUIRES_CANONICAL_MIRROR")
            self.follow_max_ms = FOLLOW_MAX_MS
            self.bucket_ms = 100
            self.min_qty = dict(MIN_QTY)
            self.material_price_bps = 0.15
            self.dual_cash_futures_optional = False
        self.guardian = _load_guardian()
        self.risk = shadow_risk_guard
        risk_ratchet_price_quality_hook.install(self.risk)
        self.residual = ResidualEdgeBook()
        self.orphan_twins = []
        self.clock = {name: ClockQuality() for name in TRADE_STREAMS.values()}
        self.flow = {name: FlowBaseline() for name in TRADE_STREAMS.values()}
        self.last_sequence = {}
        self.epochs = {name: 0 for name in TRADE_STREAMS}
        self.last_signal = {}
        self.episode = None
        self.twins = {}
        self.latest_bias = {
            "direction": "ABSTAIN", "confidence": 0.0,
            "received_ms": 0, "regime": "UNKNOWN",
        }
        self.latest_core_decision = {"decision": "WAIT", "side": "ABSTAIN", "received_ms": 0}
        self.oi_history = deque(maxlen=8)
        self.commission = {
            "maker_fee_bps": 9.0, "taker_fee_bps": 9.0,
            "verified": False, "source": "CONSERVATIVE_CONFIG_FALLBACK",
        }
        self.last_runtime_read_ms = 0
        self.last_cpu_read_ms = 0
        self.governor_mode = "NORMAL"
        self.state = SimpleNamespace(
            best_bid=0.0, best_ask=0.0, thoi_gian_tick_cuoi=0.0,
            execution_best_bid=0.0, execution_best_ask=0.0,
            execution_bbo_ts=0.0, coinbase_price=0.0,
            thoi_gian_coinbase_ticker_cuoi=0.0,
            coinbase_flow_3s_ts=0.0, coinbase_volume_3s=0.0,
            coinbase_cvd_3s=0.0, open_interest=0.0,
            open_interest_ts=0.0, atr_1m=0.0,
            flow_1s_buffer=deque(maxlen=128),
            danh_sach_khop_lenh_futures=deque(maxlen=256),
        )
        self.coinbase_flow = deque(maxlen=128)
        self.price_points = deque()
        self.price_max = deque()
        self.price_min = deque()
        self.generated_counts = {}
        self._load_state()

    def _load_state(self):
        if self.state_path is None:
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (
                payload.get("wavefront_version") != VERSION
                or payload.get("evidence_version") != self.evidence_version
            ):
                return
            self.residual = ResidualEdgeBook.from_state(payload.get("residual") or {})
            self.orphan_twins = list(payload.get("open_twins") or ())
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _persist(self):
        if self.state_path is None:
            return
        payload = {
            "schema_version": 1,
            "wavefront_version": VERSION,
            "evidence_version": self.evidence_version,
            "residual": self.residual.to_state(),
            "open_twins": [
                {
                    "causal_episode_id": twin.get("episode_id"),
                    "execution_twin": twin.get("name"),
                    "side": twin.get("side"),
                    "filled": bool(twin.get("filled")),
                    "causal_class": (twin.get("candidate") or {}).get("causal_class"),
                    "bias_relation": (twin.get("candidate") or {}).get("bias_relation"),
                    "proposer": (twin.get("candidate") or {}).get("proposer"),
                    "cost_plan": twin.get("cost_plan"),
                }
                for twin in self.twins.values() if not twin.get("terminal")
            ],
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except OSError:
            # Research durability failure cannot interrupt raw recording.
            return

    def _flush_orphans(self, now_ms):
        if not self.orphan_twins:
            return
        for twin in self.orphan_twins:
            payload = {
                "causal_episode_id": twin.get("causal_episode_id"),
                "execution_twin": twin.get("execution_twin"),
                "side": twin.get("side"), "filled": bool(twin.get("filled")),
                "valid": False, "exit_reason": "RECORDER_RESTART_GAP",
                "causal_class": twin.get("causal_class"),
                "bias_relation": twin.get("bias_relation"),
                "proposer": twin.get("proposer"),
                "cost_plan": twin.get("cost_plan"),
                "commission_verified": bool(
                    (twin.get("cost_plan") or {}).get("commission_verified")
                ),
            }
            self._publish("wavefront_virtual_exit", payload, now_ms)
            self._publish(
                "residual_edge_report", self.residual.observe_exit(payload), now_ms
            )
        self.orphan_twins = []
        self._persist()

    def _publish(self, stream, payload, event_ms):
        row = {
            "schema_version": "WAVEFRONT_RESEARCH_V1",
            "version": VERSION,
            "authority": AUTHORITY,
            **payload,
        }
        self.generated_counts[stream] = self.generated_counts.get(stream, 0) + 1
        self.emit(stream, row, event_time_ms=int(event_ms or 0))

    def _read_control_files(self, now_ms):
        if self.cpu_status_path and now_ms - self.last_cpu_read_ms >= 5_000:
            self.last_cpu_read_ms = now_ms
            try:
                data = json.loads(self.cpu_status_path.read_text(encoding="utf-8"))
                self.governor_mode = str(data.get("governor_mode") or "CONSERVE").upper()
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self.governor_mode = "CONSERVE"
        if self.runtime_health_path and now_ms - self.last_runtime_read_ms >= 30_000:
            self.last_runtime_read_ms = now_ms
            try:
                data = json.loads(self.runtime_health_path.read_text(encoding="utf-8"))
                maker = _f(data.get("mainnet_maker_fee_bps"), -1.0)
                taker = _f(data.get("mainnet_taker_fee_bps"), -1.0)
                verified = bool(data.get("mainnet_commission_verified"))
                if verified and maker >= 0.0 and taker > 0.0:
                    self.commission = {
                        "maker_fee_bps": maker, "taker_fee_bps": taker,
                        "verified": True,
                        "source": str(data.get("mainnet_commission_source") or "BOT_RUNTIME"),
                    }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

    def _observe_bot_event(self, record):
        payload = record.get("payload") or {}
        event = str(payload.get("event") or "").upper()
        body = payload.get("payload") or payload
        now_ms = int(record.get("receive_time_ms", 0) or 0)
        self.residual.observe_core_event(event, body, now_ms)
        if event in ("ENTRY", "EXIT"):
            self._persist()
        if event != "DECISION_EVALUATED":
            return
        decision = body.get("decision_record") or {}
        inputs = decision.get("inputs") or {}
        bias = inputs.get("bias") or {}
        self.latest_bias = {
            "direction": str(bias.get("direction") or body.get("side") or "ABSTAIN").upper(),
            "confidence": _f(bias.get("confidence")),
            "received_ms": now_ms,
            "regime": str(((inputs.get("regime") or {}).get("regime")) or "UNKNOWN"),
        }
        output = decision.get("output") or {}
        self.latest_core_decision = {
            "decision": str(output.get("decision") or body.get("decision") or "WAIT").upper(),
            "side": str(output.get("side") or body.get("side") or "ABSTAIN").upper(),
            "received_ms": now_ms,
        }
        components = ((output.get("cost") or {}).get("components") or {})
        if components.get("commission_verified"):
            style = str(components.get("execution_style") or "").upper()
            entry_fee = _f(components.get("entry_fee_bps"), -1.0)
            exit_fee = _f(components.get("exit_fee_bps"), -1.0)
            if exit_fee > 0.0:
                self.commission["taker_fee_bps"] = exit_fee
            if style == "MAKER" and entry_fee >= 0.0:
                self.commission["maker_fee_bps"] = entry_fee
            elif style == "TAKER" and entry_fee > 0.0:
                self.commission["taker_fee_bps"] = entry_fee
            self.commission["verified"] = True
            self.commission["source"] = str(components.get("commission_source") or "BOT_DECISION")

    def _sequence_ok(self, record):
        stream = str(record.get("stream") or "")
        if stream not in TRADE_STREAMS:
            return True
        last = self.last_sequence.get(stream)
        previous = record.get("previous_sequence")
        start = record.get("sequence_start")
        gap = bool(last is not None and (
            (previous is not None and int(previous) != last)
            or (start is not None and int(start) != last + 1)
        ))
        end = record.get("sequence_end")
        if end is not None:
            self.last_sequence[stream] = int(end)
        if gap:
            self.epochs[stream] += 1
            self._invalidate_all("EXECUTED_FLOW_SEQUENCE_GAP", int(record.get("receive_time_ms", 0) or 0))
        return not gap

    def _invalidate_all(self, reason, now_ms):
        if self.episode is not None:
            self._publish("wavefront_candidate", {
                **self._episode_payload(self.episode),
                "decision": "INVALID", "reason": reason,
            }, now_ms)
            self.episode = None
        for twin in list(self.twins.values()):
            self._finish_twin(twin, now_ms, reason, valid=False)

    def _clock_signal(self, record, venue):
        payload = record.get("payload") or {}
        event_ms = int(payload.get("last_event_time_ms") or record.get("event_time_ms", 0) or 0)
        receive_ms = int(record.get("receive_time_ms", 0) or 0)
        corrected = self.clock[venue].observe(event_ms, receive_ms)
        signal = self.flow[venue].score(
            payload, venue, self.warmup_samples,
            min_qty=self.min_qty,
            material_price_bps=self.material_price_bps,
        )
        signal.update({
            "event_time_ms": event_ms, "receive_time_ms": receive_ms,
            "corrected_event_time_ms": corrected,
            "clock_uncertainty_ms": self.clock[venue].uncertainty_ms,
            "sequence_end": record.get("sequence_end"),
            "epoch": self.epochs[str(record.get("stream"))],
            "clock_valid": self.clock[venue].last_valid,
        })
        if not signal["clock_valid"]:
            signal["strong"] = False
            self._invalidate_all("CLOCK_OR_EVENT_TIME_INVALID", receive_ms)
        if signal["strong"]:
            self.last_signal[venue] = signal
        return signal

    def _update_market_state(self, record, signal=None):
        stream = str(record.get("stream") or "")
        payload = record.get("payload") or {}
        now_ms = int(record.get("receive_time_ms", 0) or 0)
        now = now_ms / 1000.0
        if stream == "binance_spot_ticker":
            self.state.best_bid = _f(payload.get("bid", payload.get("b")))
            self.state.best_ask = _f(payload.get("ask", payload.get("a")))
            self.state.thoi_gian_tick_cuoi = now
        elif stream == "book_ticker":
            self.state.execution_best_bid = _f(payload.get("b", payload.get("bid")))
            self.state.execution_best_ask = _f(payload.get("a", payload.get("ask")))
            self.state.execution_bbo_ts = now
        elif stream == "open_interest":
            oi = _f(payload.get("openInterest"))
            if oi > 0.0:
                self.state.open_interest = oi
                self.state.open_interest_ts = now
                self.oi_history.append((now_ms, oi))
        elif signal and signal["venue"] == "binance_spot":
            self.state.flow_1s_buffer.append({
                "ts": now, "buy": signal["buy_qty"], "sell": signal["sell_qty"],
            })
            if not self.state.best_bid or not self.state.best_ask:
                self.state.best_bid = signal["price"]
                self.state.best_ask = signal["price"]
                self.state.thoi_gian_tick_cuoi = now
        elif signal and signal["venue"] == "coinbase_spot":
            self.state.coinbase_price = signal["price"]
            self.state.thoi_gian_coinbase_ticker_cuoi = now
            self.coinbase_flow.append((now, signal["buy_qty"], signal["sell_qty"]))
            while self.coinbase_flow and self.coinbase_flow[0][0] < now - 3.0:
                self.coinbase_flow.popleft()
            buy = sum(row[1] for row in self.coinbase_flow)
            sell = sum(row[2] for row in self.coinbase_flow)
            self.state.coinbase_volume_3s = buy + sell
            self.state.coinbase_cvd_3s = buy - sell
            self.state.coinbase_flow_3s_ts = now
        elif signal and signal["venue"] == "futures":
            event_ms = int(signal["event_time_ms"])
            if signal["buy_qty"] > 0.0:
                self.state.danh_sach_khop_lenh_futures.append({
                    "thoi_gian_ms": event_ms, "khoi_luong": signal["buy_qty"],
                    "ban_chu_dong": False, "gia": signal["price"],
                })
            if signal["sell_qty"] > 0.0:
                self.state.danh_sach_khop_lenh_futures.append({
                    "thoi_gian_ms": event_ms, "khoi_luong": signal["sell_qty"],
                    "ban_chu_dong": True, "gia": signal["price"],
                })
            self._update_atr(now_ms, signal["price"])

    def _update_atr(self, now_ms, price):
        if price <= 0.0:
            return
        self.price_points.append((now_ms, price))
        while self.price_max and self.price_max[-1][1] <= price:
            self.price_max.pop()
        while self.price_min and self.price_min[-1][1] >= price:
            self.price_min.pop()
        self.price_max.append((now_ms, price))
        self.price_min.append((now_ms, price))
        cutoff = now_ms - 60_000
        while self.price_points and self.price_points[0][0] < cutoff:
            self.price_points.popleft()
        while self.price_max and self.price_max[0][0] < cutoff:
            self.price_max.popleft()
        while self.price_min and self.price_min[0][0] < cutoff:
            self.price_min.popleft()
        if self.price_max and self.price_min:
            self.state.atr_1m = self.price_max[0][1] - self.price_min[0][1]

    def _oi_intent(self, now_ms):
        if len(self.oi_history) < 2 or now_ms - self.oi_history[-1][0] > 20_000:
            return {"intent": "UNKNOWN", "fresh": False, "change_pct": None}
        previous = self.oi_history[-2][1]
        current = self.oi_history[-1][1]
        change = (current - previous) / previous * 100.0 if previous > 0.0 else 0.0
        intent = "POSITION_BUILD" if change >= 0.02 else "UNWIND" if change <= -0.02 else "NEUTRAL"
        return {"intent": intent, "fresh": True, "change_pct": round(change, 6)}

    def _start_episode(self, signal):
        side = signal["side"]
        other_cash = "coinbase_spot" if signal["venue"] == "binance_spot" else "binance_spot"
        opposing_cash = self.last_signal.get(other_cash)
        if (
            opposing_cash and opposing_cash["side"] != side
            and 0 <= signal["receive_time_ms"] - opposing_cash["receive_time_ms"] <= self.follow_max_ms
        ):
            self._publish("wavefront_candidate", {
                "causal_episode_id": self._episode_id(signal),
                "decision": "REJECT", "reason": "CASH_GROUP_OPPOSED",
                "side": side, "proposer": signal["venue"],
            }, signal["receive_time_ms"])
            return
        futures = self.last_signal.get("futures")
        if futures and futures["side"] == side:
            delta = signal["corrected_event_time_ms"] - futures["corrected_event_time_ms"]
            if 0.0 <= delta <= self.follow_max_ms:
                self._publish("wavefront_candidate", {
                    "causal_episode_id": self._episode_id(signal),
                    "decision": "REJECT", "reason": "FUTURES_LED",
                    "side": side, "proposer": signal["venue"],
                    "futures_lead_ms": round(delta, 4),
                    "authority": False,
                }, signal["receive_time_ms"])
                return
        episode = {
            "id": self._episode_id(signal), "side": side,
            "proposer": signal["venue"], "proposal": dict(signal),
            "started_receive_ms": signal["receive_time_ms"],
            "cash_hits": {signal["venue"]: 1},
            "cash_signals": {signal["venue"]: dict(signal)},
            "bias_snapshot": dict(self.latest_bias),
            "core_snapshot": dict(self.latest_core_decision),
        }
        self.episode = episode
        self._publish("wavefront_candidate", {
            **self._episode_payload(episode),
            "decision": "PROPOSED", "reason": "CASH_FLOW_PRICE_CONVERSION",
        }, signal["receive_time_ms"])

    @staticmethod
    def _episode_id(signal):
        sequence = signal.get("sequence_end")
        token = sequence if sequence is not None else signal["event_time_ms"]
        return f"wf:{signal['venue']}:{signal['side']}:{token}"

    def _episode_payload(self, episode):
        proposal = episode["proposal"]
        return {
            "causal_episode_id": episode["id"], "side": episode["side"],
            "proposer": episode["proposer"],
            "onset_signature": self.residual.onset_signature(
                episode["side"], episode["proposer"],
                episode["started_receive_ms"],
            ),
            "proposer_event_time_ms": proposal["event_time_ms"],
            "proposer_receive_time_ms": proposal["receive_time_ms"],
            "proposer_corrected_event_time_ms": round(proposal["corrected_event_time_ms"], 4),
            "proposer_flow": {k: proposal.get(k) for k in (
                "imbalance", "total_qty", "signed_quote", "price",
                "price_conversion_bps", "surprise_ratio", "baseline_samples",
            )},
            "bias_snapshot": episode["bias_snapshot"],
            "core_snapshot": episode["core_snapshot"],
            "cash_hits": dict(episode["cash_hits"]),
            "clock_quality": {
                name: quality.snapshot() for name, quality in self.clock.items()
            },
        }

    def _observe_cash_signal(self, signal):
        if not signal["strong"]:
            return
        if self.episode is None:
            self._start_episode(signal)
            return
        episode = self.episode
        if signal["receive_time_ms"] - episode["started_receive_ms"] > self.follow_max_ms:
            self._expire_episode(signal["receive_time_ms"], "FOLLOW_WINDOW_EXPIRED")
            self._start_episode(signal)
            return
        if signal["side"] != episode["side"]:
            self._expire_episode(signal["receive_time_ms"], "OPPOSING_CASH_FLOW")
            return
        episode["cash_hits"][signal["venue"]] = episode["cash_hits"].get(signal["venue"], 0) + 1
        episode["cash_signals"][signal["venue"]] = dict(signal)
        if (
            self.dual_cash_futures_optional
            and len(episode["cash_hits"]) >= 2
        ):
            self._qualify_episode(signal, confirmation="DUAL_FRESH_CASH")

    def _observe_futures_signal(self, signal):
        if not signal["strong"] or self.episode is None:
            return
        episode = self.episode
        if signal["side"] != episode["side"]:
            self._expire_episode(signal["receive_time_ms"], "OPPOSING_FUTURES_FLOW")
            return
        receive_delta = signal["receive_time_ms"] - episode["started_receive_ms"]
        if receive_delta > self.follow_max_ms:
            self._expire_episode(signal["receive_time_ms"], "FOLLOW_WINDOW_EXPIRED")
            return
        proposal = episode["proposal"]
        corrected_delta = signal["corrected_event_time_ms"] - proposal["corrected_event_time_ms"]
        uncertainty = signal["clock_uncertainty_ms"] + proposal["clock_uncertainty_ms"]
        lead_lower_bound = corrected_delta - uncertainty
        if corrected_delta < 0.0:
            self._expire_episode(signal["receive_time_ms"], "FUTURES_LED")
            return
        if lead_lower_bound < LEAD_FLOOR_MS:
            self._publish("wavefront_candidate", {
                **self._episode_payload(episode),
                "decision": "WAIT", "reason": "CAUSAL_LEAD_UNCERTAIN",
                "corrected_follow_ms": round(corrected_delta, 4),
                "lead_lower_bound_ms": round(lead_lower_bound, 4),
            }, signal["receive_time_ms"])
            return

        self._qualify_episode(
            signal,
            confirmation="FUTURES_FOLLOWER",
            corrected_follow_ms=corrected_delta,
            lead_lower_bound_ms=lead_lower_bound,
        )

    def _qualify_episode(
        self, signal, *, confirmation, corrected_follow_ms=None,
        lead_lower_bound_ms=None,
    ):
        """Finish one mirror episode without granting production authority."""
        if self.episode is None:
            return
        episode = self.episode
        if not self._feeds_ready():
            self._publish("wavefront_candidate", {
                **self._episode_payload(episode),
                "decision": "WAIT", "reason": "FEED_GROUP_NOT_READY",
            }, signal["receive_time_ms"])
            return
        oi = self._oi_intent(signal["receive_time_ms"])
        bias_direction = str(episode["bias_snapshot"].get("direction") or "ABSTAIN").upper()
        bias_fresh = bool(
            0 <= signal["receive_time_ms"] - int(
                episode["bias_snapshot"].get("received_ms", 0) or 0
            ) <= 90_000
        )
        if not bias_fresh:
            bias_direction = "ABSTAIN"
        bias_relation = (
            "ALIGNED" if bias_direction == episode["side"]
            else "REVERSAL" if bias_direction in ("LONG", "SHORT")
            else "NEUTRAL"
        )
        cash_persistence = sum(episode["cash_hits"].values())
        two_cash = len(episode["cash_hits"]) >= 2
        causal_class = None
        if bias_relation == "ALIGNED" and oi["intent"] == "POSITION_BUILD":
            causal_class = "ALIGNED_BUILD"
        elif oi["intent"] == "UNWIND" and (two_cash or cash_persistence >= 2):
            causal_class = "CASH_LED_UNWIND"
        elif bias_relation == "REVERSAL" and oi["intent"] == "POSITION_BUILD" and two_cash:
            self._publish("wavefront_candidate", {
                **self._episode_payload(episode),
                "decision": "RESEARCH_ONLY", "reason": "REVERSAL_BUILD_NOT_AUTHORIZED",
                "bias_relation": bias_relation, "oi_intent": oi,
                "bias_fresh": bias_fresh,
                "confirmation": confirmation,
                "corrected_follow_ms": (
                    round(corrected_follow_ms, 4)
                    if corrected_follow_ms is not None else None
                ),
                "lead_lower_bound_ms": (
                    round(lead_lower_bound_ms, 4)
                    if lead_lower_bound_ms is not None else None
                ),
            }, signal["receive_time_ms"])
            self.episode = None
            return
        else:
            self._publish("wavefront_candidate", {
                **self._episode_payload(episode),
                "decision": "WAIT", "reason": "OI_OR_BIAS_CLASS_NOT_READY",
                "bias_relation": bias_relation, "oi_intent": oi,
                "bias_fresh": bias_fresh,
                "confirmation": confirmation,
                "corrected_follow_ms": (
                    round(corrected_follow_ms, 4)
                    if corrected_follow_ms is not None else None
                ),
                "lead_lower_bound_ms": (
                    round(lead_lower_bound_ms, 4)
                    if lead_lower_bound_ms is not None else None
                ),
            }, signal["receive_time_ms"])
            return

        futures = self.last_signal.get("futures")
        futures_fresh_aligned = bool(
            futures
            and futures.get("side") == episode["side"]
            and 0 <= signal["receive_time_ms"] - int(
                futures.get("receive_time_ms", 0) or 0
            ) <= self.follow_max_ms
        )
        candidate = {
            **self._episode_payload(episode),
            "decision": "QUALIFIED",
            "reason": (
                "DUAL_FRESH_CASH_FUTURES_OPTIONAL"
                if confirmation == "DUAL_FRESH_CASH"
                else "CASH_PROPOSER_FUTURES_FOLLOWER"
            ),
            "causal_class": causal_class, "bias_relation": bias_relation,
            "bias_fresh": bias_fresh,
            "oi_intent": oi,
            "confirmation": confirmation,
            "futures_verifier": (
                "ALIGNED_FRESH" if futures_fresh_aligned else "NOT_REQUIRED"
            ),
            "futures_flow": (
                dict(signal) if confirmation == "FUTURES_FOLLOWER"
                else dict(futures or {})
            ),
            "corrected_follow_ms": (
                round(corrected_follow_ms, 4)
                if corrected_follow_ms is not None else None
            ),
            "receive_follow_ms": (
                signal["receive_time_ms"] - episode["started_receive_ms"]
            ),
            "lead_lower_bound_ms": (
                round(lead_lower_bound_ms, 4)
                if lead_lower_bound_ms is not None else None
            ),
            "exchange_independence": {
                "cash_group": sorted(episode["cash_hits"]),
                "derivative_group": ["futures", "open_interest"],
                "price_3venue_counts_as_one": True,
            },
        }
        self.residual.observe_candidate(signal["receive_time_ms"])
        self._persist()
        self._publish("wavefront_candidate", candidate, signal["receive_time_ms"])
        self.episode = None
        if self.twins:
            self._publish("wavefront_candidate", {
                **candidate, "decision": "SKIP", "reason": "WOULD_SKIP_POSITION_OPEN",
            }, signal["receive_time_ms"])
            return
        self._open_twins(candidate, signal["receive_time_ms"])

    def _feeds_ready(self):
        if callable(self.feed_health):
            try:
                status = dict(self.feed_health() or {})
            except Exception:
                return False
            return all(bool(status.get(name)) for name in (
                "binance_spot_ws", "coinbase_spot_ws", "market_ws"
            ))
        # Deterministic replay has no process connection map. Requiring a
        # warmed, continuous epoch from every authority stream is its equivalent.
        return all(self.clock[name].samples >= 5 for name in (
            "binance_spot", "coinbase_spot", "futures"
        ))

    def _expire_episode(self, now_ms, reason):
        if self.episode is None:
            return
        self._publish("wavefront_candidate", {
            **self._episode_payload(self.episode),
            "decision": "EXPIRED", "reason": reason,
        }, now_ms)
        self.episode = None

    def _cost_plan(self, style):
        bid = self.state.execution_best_bid
        ask = self.state.execution_best_ask
        mid = _mid(bid, ask)
        half_spread = (ask - bid) / mid * 5_000.0 if mid > 0.0 else 1.0
        market_slippage = 1.5
        maker = _f(self.commission.get("maker_fee_bps"), 9.0)
        taker = _f(self.commission.get("taker_fee_bps"), 9.0)
        is_maker = style == "MAKER_TWIN"
        if self.profile == CANONICAL_MIRROR_PROFILE:
            cost_state = SimpleNamespace(
                execution_best_bid=bid,
                execution_best_ask=ask,
                mainnet_maker_fee_bps=maker,
                mainnet_taker_fee_bps=taker,
                mainnet_commission_verified=bool(
                    self.commission.get("verified")
                ),
                mainnet_commission_source=self.commission.get("source"),
            )
            plan = verified_cost_model.shadow_execution_plan(
                {"phase": "ACCEPTANCE" if is_maker else "RELEASE"},
                cost_state,
                "MAKER_TRADE_THROUGH" if is_maker else "MARKET",
            )
            return {
                **plan,
                "execution_twin": style,
                "promotion_cost_verified": bool(
                    plan.get("commission_verified")
                ),
            }
        entry_fee = maker if is_maker else taker
        entry_slippage = 0.0 if is_maker else half_spread + market_slippage
        exit_slippage = half_spread + market_slippage
        total = entry_fee + taker + entry_slippage + exit_slippage
        return {
            "execution_twin": style,
            "commission_verified": bool(self.commission.get("verified")),
            "commission_source": self.commission.get("source"),
            "entry_fee_bps": round(entry_fee, 6),
            "exit_fee_bps": round(taker, 6),
            "half_spread_bps": round(max(0.0, half_spread), 6),
            "entry_slippage_bps": round(entry_slippage, 6),
            "exit_slippage_bps": round(exit_slippage, 6),
            "total_cost_bps": round(total, 6),
            "minimum_net_edge_bps": 2.0 if is_maker else 6.0,
            "promotion_cost_verified": bool(self.commission.get("verified")),
        }

    def _open_twins(self, candidate, now_ms):
        bid = self.state.execution_best_bid
        ask = self.state.execution_best_ask
        if bid <= 0.0 or ask <= bid or now_ms / 1000.0 - self.state.execution_bbo_ts > 1.0:
            self._publish("wavefront_candidate", {
                **candidate, "decision": "SKIP", "reason": "STALE_EXECUTABLE_BBO",
            }, now_ms)
            return
        for name in ("MAKER_TWIN", "TAKER_TWIN"):
            cost = self._cost_plan(name)
            twin = {
                "name": name, "candidate": candidate, "cost_plan": cost,
                "side": candidate["side"], "episode_id": candidate["causal_episode_id"],
                "created_ms": now_ms, "expires_ms": now_ms + MAKER_TTL_MS,
                "limit_price": bid if candidate["side"] == "LONG" else ask,
                "filled": False, "terminal": False, "trace": [],
                "mfe_bps": 0.0, "mae_bps": 0.0,
                "adverse_250ms_bps": None, "adverse_1s_bps": None,
                "time_to_positive_net_ms": None,
            }
            self.twins[name] = twin
            if name == "TAKER_TWIN":
                mid = _mid(bid, ask)
                half_spread = (
                    (ask - bid) / mid * 5_000.0 if mid > 0.0 else 0.0
                )
                slip = max(
                    0.0,
                    _f(cost.get("entry_slippage_bps")) - half_spread,
                )
                fill = ask * (1.0 + slip / 10_000.0) if twin["side"] == "LONG" else bid * (1.0 - slip / 10_000.0)
                self._fill_twin(twin, fill, now_ms, "IMMEDIATE_EXECUTABLE_BBO")
        self._persist()

    def _fill_twin(self, twin, price, now_ms, fill_reason):
        if twin["filled"] or twin["terminal"] or price <= 0.0:
            return
        candidate = twin["candidate"]
        pos = SimpleNamespace(
            active=True, side=twin["side"], qty=QTY_BTC,
            entry_price=float(price), opened_at=now_ms / 1000.0,
            position_cycle_id=f"{twin['episode_id']}:{twin['name']}",
            entry_causal_thesis={
                "primary_cash_anchor": candidate["proposer"].replace("binance_", "").replace("_spot", ""),
                "cash_anchors": [name.replace("binance_", "").replace("_spot", "") for name in candidate["cash_hits"]],
            },
            guardian_s_candidate_since=0.0, guardian_s_signature=(),
            guardian_s_scout_since=0.0,
        )
        self.risk.arm(pos, price)
        recovery_cost = _f(
            twin["cost_plan"].get("remaining_recovery_cost_bps"),
            twin["cost_plan"].get("total_cost_bps"),
        )
        pos.fee_r = recovery_cost / (STOP_FRACTION * 10_000.0)
        twin.update({
            "filled": True, "fill_ms": now_ms, "entry_price": float(price),
            "position": pos, "state": SimpleNamespace(), "fill_reason": fill_reason,
        })
        self._sync_twin_state(twin)
        self._publish("wavefront_virtual_entry", {
            "causal_episode_id": twin["episode_id"], "execution_twin": twin["name"],
            "side": twin["side"], "fill_price": float(price), "qty_btc": QTY_BTC,
            "fill_reason": fill_reason, "cost_plan": twin["cost_plan"],
            "causal_class": candidate["causal_class"],
            "bias_relation": candidate["bias_relation"],
            "proposer": candidate["proposer"],
            "guardian_version": self.guardian.VERSION,
            "risk_version": self.risk.VERSION,
            "economic_contract_version": "ENTRY_ECONOMICS_V7_CAUSAL_PROOF_SEMANTICS",
            "hard_sl": pos.hard_sl, "core_snapshot": candidate["core_snapshot"],
        }, now_ms)
        self._persist()

    def _sync_twin_state(self, twin):
        target = twin["state"]
        for name, value in vars(self.state).items():
            if name.startswith("guardian_s_"):
                continue
            setattr(target, name, value)

    def _maybe_fill_maker(self, signal):
        twin = self.twins.get("MAKER_TWIN")
        if not twin or twin["filled"] or twin["terminal"]:
            return
        now_ms = signal["receive_time_ms"]
        if now_ms > twin["expires_ms"]:
            self._finish_twin(twin, now_ms, "MAKER_TTL_EXPIRED", valid=False)
            return
        limit_price = twin["limit_price"]
        if twin["side"] == "LONG":
            trade_through = _f(signal.get("low"), signal["price"]) <= limit_price
            volume = signal["sell_qty"]
        else:
            trade_through = _f(signal.get("high"), signal["price"]) >= limit_price
            volume = signal["buy_qty"]
        if trade_through and volume + 1e-12 >= QTY_BTC:
            self._fill_twin(twin, limit_price, now_ms, "EXECUTED_TRADE_THROUGH")

    def _close_price(self, side):
        bid, ask = self.state.execution_best_bid, self.state.execution_best_ask
        slip = 1.5 / 10_000.0
        return bid * (1.0 - slip) if side == "LONG" else ask * (1.0 + slip)

    def _advance_twins(self, now_ms):
        for twin in list(self.twins.values()):
            if twin["terminal"]:
                continue
            if not twin["filled"]:
                if twin["name"] == "MAKER_TWIN" and now_ms > twin["expires_ms"]:
                    self._finish_twin(twin, now_ms, "MAKER_TTL_EXPIRED", valid=False)
                continue
            if now_ms / 1000.0 - self.state.execution_bbo_ts > 5.0:
                self._finish_twin(twin, now_ms, "STALE_EXECUTABLE_BBO", valid=False)
                continue
            self._sync_twin_state(twin)
            state = twin["state"]
            if now_ms / 1000.0 - _f(self.state.open_interest_ts) > 20.0:
                state.open_interest = 0.0
            pos = twin["position"]
            px = self._close_price(twin["side"])
            if px <= 0.0:
                continue
            signed_bps = _side_sign(twin["side"]) * (px - twin["entry_price"]) / twin["entry_price"] * 10_000.0
            twin["mfe_bps"] = max(twin["mfe_bps"], signed_bps)
            twin["mae_bps"] = min(twin["mae_bps"], signed_bps)
            age = now_ms - twin["fill_ms"]
            # Entry/exit slippage is already embedded in executable fill/close
            # prices. Subtract commissions exactly once when measuring the
            # first moment the twin becomes positive after all frozen cost.
            executable_net_bps = signed_bps - (
                _f(twin["cost_plan"].get("entry_fee_bps"))
                + _f(twin["cost_plan"].get("exit_fee_bps"))
            )
            if (
                twin.get("time_to_positive_net_ms") is None
                and executable_net_bps >= 0.0
            ):
                twin["time_to_positive_net_ms"] = max(0, age)
            if age >= 250 and twin["adverse_250ms_bps"] is None:
                twin["adverse_250ms_bps"] = min(0.0, signed_bps)
            if age >= 1_000 and twin["adverse_1s_bps"] is None:
                twin["adverse_1s_bps"] = min(0.0, signed_bps)
            guardian = self.guardian.update_state(state, pos, now=now_ms / 1000.0)
            risk = self.risk.assess(pos, px, guardian=guardian, market_state=state, now=now_ms / 1000.0)
            trace_key = (guardian.get("decision"), guardian.get("reason"), risk.get("decision"), risk.get("reason"))
            if not twin["trace"] or twin["trace"][-1].get("key") != trace_key:
                twin["trace"].append({
                    "key": trace_key, "at_ms": now_ms,
                    "guardian": guardian, "risk": risk,
                })
                if len(twin["trace"]) > 64:
                    twin["trace"].pop(0)
            if risk.get("decision") == "EXIT":
                self._finish_twin(twin, now_ms, str(risk.get("reason") or "RISK_EXIT"), valid=True, exit_price=px, guardian=guardian, risk=risk)
            elif guardian.get("decision") == "EXIT" and self.risk.guardian_ok(guardian):
                self._finish_twin(twin, now_ms, str(guardian.get("reason") or "GUARDIAN_EXIT"), valid=True, exit_price=px, guardian=guardian, risk=risk)

    def _finish_twin(self, twin, now_ms, reason, *, valid, exit_price=None, guardian=None, risk=None):
        if twin.get("terminal"):
            return
        twin["terminal"] = True
        filled = bool(twin.get("filled"))
        candidate = twin["candidate"]
        payload = {
            "causal_episode_id": twin["episode_id"],
            "execution_twin": twin["name"], "side": twin["side"],
            "filled": filled, "valid": bool(valid and filled),
            "exit_reason": reason, "causal_class": candidate["causal_class"],
            "bias_relation": candidate["bias_relation"],
            "proposer": candidate["proposer"],
            "regime": candidate["bias_snapshot"].get("regime", "UNKNOWN"),
            "cost_plan": twin["cost_plan"],
            "commission_verified": twin["cost_plan"]["commission_verified"],
            "guardian_version": self.guardian.VERSION,
            "risk_version": self.risk.VERSION,
            "economic_contract_version": "ENTRY_ECONOMICS_V7_CAUSAL_PROOF_SEMANTICS",
        }
        if filled and exit_price is not None:
            entry = twin["entry_price"]
            gross_bps = _side_sign(twin["side"]) * (float(exit_price) - entry) / entry * 10_000.0
            fee_bps = twin["cost_plan"]["entry_fee_bps"] + twin["cost_plan"]["exit_fee_bps"]
            net_bps = gross_bps - fee_bps
            roundtrip_cost = _f(
                twin["cost_plan"].get("roundtrip_cost_bps"),
                twin["cost_plan"].get("total_cost_bps"),
            )
            threshold = roundtrip_cost + twin["cost_plan"]["minimum_net_edge_bps"]
            economic_wave = twin["mfe_bps"] >= threshold
            advance = self.residual.match_core_entry(
                twin["side"], twin["fill_ms"], now_ms,
                causal_wave_id=candidate.get("causal_episode_id"),
                onset_signature=candidate.get("onset_signature"),
            )
            core_shared = advance is not None
            payload.update({
                "entry_price": entry, "exit_price": float(exit_price),
                "fill_time_ms": twin["fill_ms"], "exit_time_ms": now_ms,
                "holding_time_seconds": max(0.0, (now_ms - twin["fill_ms"]) / 1000.0),
                "time_to_positive_net_ms": twin.get("time_to_positive_net_ms"),
                "time_to_positive_net_seconds": (
                    round(twin["time_to_positive_net_ms"] / 1000.0, 6)
                    if twin.get("time_to_positive_net_ms") is not None else None
                ),
                "gross_pnl_bps": round(gross_bps, 6),
                "net_pnl_bps": round(net_bps, 6),
                "stress_25bps_net_bps": round(gross_bps - 25.0, 6),
                "mfe_bps": round(twin["mfe_bps"], 6),
                "mae_bps": round(twin["mae_bps"], 6),
                "adverse_selection_250ms_bps": twin["adverse_250ms_bps"],
                "adverse_selection_1s_bps": twin["adverse_1s_bps"],
                "economic_threshold_bps": round(threshold, 6),
                "economic_wave": economic_wave,
                "capture_ratio": round(
                    max(0.0, min(1.0, net_bps / max(twin["mfe_bps"], 1e-9))), 6
                ),
                "core_shared_entry": core_shared,
                "entry_advance_ms": advance,
                "guardian": guardian, "risk": risk,
                "guardian_trace": twin["trace"],
                "net_cost_accounting": (
                    "EXECUTABLE_FILL_PRICES_INCLUDE_SLIPPAGE_FEES_SUBTRACTED_ONCE"
                ),
            })
        self._publish("wavefront_virtual_exit", payload, now_ms)
        report = self.residual.observe_exit(payload)
        self._publish("residual_edge_report", report, now_ms)
        self.twins.pop(twin["name"], None)
        self._persist()

    def observe(self, record):
        stream = str(record.get("stream") or "")
        if stream in GENERATED_STREAMS:
            return
        now_ms = int(record.get("receive_time_ms", 0) or 0)
        if now_ms <= 0:
            return
        self._flush_orphans(now_ms)
        self._read_control_files(now_ms)
        if stream == "bot_event":
            self._observe_bot_event(record)
        sequence_ok = self._sequence_ok(record)
        signal = None
        if stream in TRADE_STREAMS and sequence_ok:
            signal = self._clock_signal(record, TRADE_STREAMS[stream])
        self._update_market_state(record, signal)

        if self.governor_mode in ("DEFENSIVE", "SAFETY_ONLY"):
            self._invalidate_all(f"CPU_{self.governor_mode}", now_ms)
            return
        if self.episode and now_ms - self.episode["started_receive_ms"] > self.follow_max_ms:
            self._expire_episode(now_ms, "FOLLOW_WINDOW_EXPIRED")
        if signal:
            if signal["venue"] in CASH_VENUES:
                self._observe_cash_signal(signal)
            else:
                self._maybe_fill_maker(signal)
                self._observe_futures_signal(signal)
        self._advance_twins(now_ms)

    def summary(self):
        return {
            "version": VERSION, "authority": False,
            "profile": self.profile,
            "canonical_mirror_version": (
                CANONICAL_MIRROR_VERSION
                if self.profile == CANONICAL_MIRROR_PROFILE else None
            ),
            "contract": {
                "bucket_ms": self.bucket_ms,
                "follow_max_ms": self.follow_max_ms,
                "min_qty": dict(self.min_qty),
                "material_price_bps": self.material_price_bps,
                "cost_plan_version": verified_cost_model.FROZEN_COST_PLAN_VERSION,
                "guardian_version": self.guardian.VERSION,
                "quantity_btc": QTY_BTC,
            },
            "ablation": dict(self.ablation),
            "governor_mode": self.governor_mode,
            "generated_counts": dict(self.generated_counts),
            "active_episode": self.episode["id"] if self.episode else None,
            "active_twins": sorted(self.twins),
            "commission": dict(self.commission),
            "residual_edge": self.residual.report(),
        }
