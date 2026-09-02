"""Recorder/replay executed depletion/refill research.

Static walls and disappearing levels are never treated as execution evidence.
Rows from this analyzer remain correlated observations until an ablation proves
incremental value beyond price and executed flow.
"""

from __future__ import annotations

from collections import deque

from recorder.depth import DepthGap, LocalOrderBook
from loi_he_thong.market_event_contract import available_time_ms


VERSION = "LIQUIDITY_RESPONSE_RESEARCH_V3"
HORIZONS_MS = (250, 1_000, 3_000)
SPOT_VERSION = "SPOT_LIQUIDITY_RESPONSE_RESEARCH_V2_CAUSAL_ORDER"
COINBASE_VERSION = "COINBASE_LIQUIDITY_RESPONSE_RESEARCH_V1_CAUSAL_ORDER"
SPOT_HORIZONS_MS = (50, 100, 250, 500)


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class LiquidityResponseAnalyzer:
    def __init__(self, emit, max_pending=64):
        self.emit = emit
        self.book = LocalOrderBook()
        self.synced = False
        self.pending = deque(maxlen=max(1, int(max_pending)))
        self.completed = 0
        self.invalid = 0

    @staticmethod
    def _levels(book, side, limit):
        source = book.asks if side == "LONG" else book.bids
        rows = []
        for price_text, qty_text in source.items():
            price, qty = _f(price_text), _f(qty_text)
            if price <= 0.0 or qty <= 0.0:
                continue
            inside = price <= limit if side == "LONG" else price >= limit
            if inside:
                rows.append((price, qty))
        return dict(rows)

    @staticmethod
    def _qty(levels):
        return sum(float(value) for value in levels.values())

    def _emit(self, tracker, now_ms, valid=True, reason="COMPLETE"):
        before = tracker["before_levels"]
        after = tracker.get("latest_levels") or {}
        current_depletion = sum(
            max(0.0, qty - after.get(price, 0.0))
            for price, qty in before.items()
        )
        depletion = max(current_depletion, _f(tracker.get("max_depletion_qty")))
        executed = tracker["executed_qty"]
        ticker = self.book.best_ticker()
        current_mid = (
            (_f(ticker.get("b")) + _f(ticker.get("a"))) / 2.0
            if ticker else 0.0
        )
        pre_mid = _f(tracker.get("pre_mid"))
        direction = 1.0 if tracker["side"] == "LONG" else -1.0
        signed_move_bps = (
            (current_mid - pre_mid) / pre_mid * 10_000.0 * direction
            if current_mid > 0.0 and pre_mid > 0.0 else None
        )
        refill_1s = tracker["refill_ratio"].get(1_000)
        absorption_candidate = bool(
            valid and executed > 0.0 and depletion > 0.0
            and refill_1s is not None and _f(refill_1s) >= 0.5
            and signed_move_bps is not None and signed_move_bps <= 0.5
        )
        payload = {
            "schema_version": "WAVEFRONT_RESEARCH_V1",
            "version": VERSION, "authority": False,
            "valid": bool(valid), "reason": reason,
            "event_time_ms": tracker["start_ms"], "side": tracker["side"],
            "executed_qty": executed, "execution_limit_price": tracker["limit"],
            "pre_event_opposite_qty": self._qty(before),
            "correlated_depletion_qty": round(depletion, 9),
            "executed_depletion_ratio": round(
                min(1.0, depletion / executed) if executed > 0.0 else 0.0, 6
            ),
            "refill_ratio": {
                str(horizon): tracker["refill_ratio"].get(horizon)
                for horizon in HORIZONS_MS
            },
            "refill_half_life_ms": tracker.get("refill_half_life_ms"),
            "pre_mid": pre_mid or None, "post_mid": current_mid or None,
            "signed_price_move_bps": (
                round(signed_move_bps, 6) if signed_move_bps is not None else None
            ),
            "absorption_candidate": absorption_candidate,
            "absorption_definition": (
                "EXECUTED_DEPLETION_PLUS_REFILL_WITHOUT_PRICE_PROGRESS"
            ),
            "cancel_is_execution": False,
            "evidence_status": "EXECUTED_CORRELATED_NOT_CAUSALLY_PROVEN",
            "eligible_for_live_gate": False,
        }
        self.emit("liquidity_response", payload, event_time_ms=int(now_ms))
        if valid:
            self.completed += 1
        else:
            self.invalid += 1

    def _invalidate(self, now_ms, reason):
        while self.pending:
            self._emit(self.pending.popleft(), now_ms, valid=False, reason=reason)
        self.synced = False

    def reset(self, now_ms, reason="DEPTH_EPOCH_RESET"):
        """Invalidate pending research across a reconnect or sequence gap."""
        self._invalidate(int(now_ms), reason)
        self.book = LocalOrderBook()

    def _update_pending(self, now_ms):
        keep = deque(maxlen=self.pending.maxlen)
        completed = []
        for tracker in self.pending:
            current = self._levels(self.book, tracker["side"], tracker["limit"])
            tracker["latest_levels"] = current
            pre_qty = self._qty(tracker["before_levels"])
            current_qty = self._qty(current)
            elapsed = now_ms - tracker["start_ms"]
            post_min_qty = min(
                _f(tracker.get("post_min_qty"), pre_qty), current_qty
            )
            tracker["post_min_qty"] = post_min_qty
            depletion = max(0.0, pre_qty - post_min_qty)
            refill_qty = max(0.0, current_qty - post_min_qty)
            refill_fraction = (
                min(1.0, refill_qty / depletion) if depletion > 0.0 else 0.0
            )
            if depletion > 0.0:
                tracker["depletion_seen"] = True
                tracker["max_depletion_qty"] = depletion
            for horizon in HORIZONS_MS:
                if elapsed >= horizon and horizon not in tracker["refill_ratio"]:
                    tracker["refill_ratio"][horizon] = round(
                        refill_fraction, 6
                    )
            if (
                tracker.get("depletion_seen") and refill_fraction >= 0.5
                and tracker.get("refill_half_life_ms") is None
            ):
                tracker["refill_half_life_ms"] = max(0, elapsed)
            if elapsed >= HORIZONS_MS[-1]:
                completed.append(tracker)
            else:
                keep.append(tracker)
        self.pending = keep
        # Detach completed trackers before callbacks can emit another record.
        for tracker in completed:
            self._emit(tracker, now_ms)

    def observe(self, record):
        stream = str(record.get("stream") or "")
        if stream in ("liquidity_response", "spot_liquidity_response"):
            return
        payload = record.get("payload") or {}
        now_ms = available_time_ms(record)
        if now_ms <= 0:
            return

        if stream in ("depth_snapshot", "depth_checkpoint"):
            if stream == "depth_checkpoint":
                self.book.reset_checkpoint(payload)
                self.synced = True
            else:
                self.book.reset(payload)
                self.synced = False
            self._update_pending(now_ms)
            return
        if stream == "depth_diff" and (
            payload.get("partial") or self.book.snapshot_update_id is not None
        ):
            try:
                status = (
                    self.book.apply_partial(payload)
                    if payload.get("partial") else self.book.apply(payload)
                )
                self.synced = status == "APPLIED" or self.book.synced
            except DepthGap:
                self._invalidate(now_ms, "DEPTH_SEQUENCE_GAP")
                return
            self._update_pending(now_ms)
            return

        self._update_pending(now_ms)
        if stream != "futures_trade_100ms" or not self.synced:
            return
        buy, sell = _f(payload.get("buy_qty")), _f(payload.get("sell_qty"))
        total = buy + sell
        if total <= 0.0:
            return
        imbalance = (buy - sell) / total
        if abs(imbalance) < 0.20:
            return
        side = "LONG" if imbalance > 0.0 else "SHORT"
        executed = buy if side == "LONG" else sell
        limit = _f(payload.get("high") if side == "LONG" else payload.get("low"))
        before = self._levels(self.book, side, limit)
        if executed <= 0.0 or limit <= 0.0 or not before:
            return
        ticker = self.book.best_ticker()
        pre_mid = (
            (_f(ticker.get("b")) + _f(ticker.get("a"))) / 2.0
            if ticker else 0.0
        )
        self.pending.append({
            "start_ms": now_ms, "side": side, "executed_qty": executed,
            "limit": limit, "before_levels": before,
            "latest_levels": dict(before), "refill_ratio": {},
            "refill_half_life_ms": None, "depletion_seen": False,
            "pre_mid": pre_mid, "max_depletion_qty": 0.0,
            "post_min_qty": self._qty(before),
        })

    def summary(self):
        return {
            "version": VERSION, "authority": False,
            "completed": self.completed, "invalid": self.invalid,
            "pending": len(self.pending), "eligible_for_live_gate": False,
        }


class SpotLiquidityResponseAnalyzer:
    """Event-conditioned Spot top-5 response without raw-depth WAL volume.

    Every top-5 update is consumed in memory.  Only an immutable derived row is
    stored after an executed-flow impulse, so the recorder preserves queue,
    microprice and spread response while remaining bounded and non-authoritative.
    """

    def __init__(
        self, emit, max_pending=64, *, venue="binance_spot",
        depth_stream="binance_spot_depth5",
        trade_stream="binance_spot_trade_100ms",
        output_stream="spot_liquidity_response", version=SPOT_VERSION,
    ):
        self.emit = emit
        self.venue = str(venue)
        self.depth_stream = str(depth_stream)
        self.trade_stream = str(trade_stream)
        self.output_stream = str(output_stream)
        self.version = str(version)
        self.pending = deque(maxlen=max(1, int(max_pending)))
        self.book = None
        self.book_history = deque(maxlen=32)
        self.completed = 0
        self.invalid = 0

    @staticmethod
    def _levels(rows):
        result = []
        for row in list(rows or [])[:5]:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price, qty = _f(row[0]), _f(row[1])
            if price > 0.0 and qty >= 0.0:
                result.append((price, qty))
        return result

    @classmethod
    def _snapshot(cls, payload):
        bids = cls._levels(payload.get("bids"))
        asks = cls._levels(payload.get("asks"))
        if not bids or not asks:
            return None
        bid, bid_qty = bids[0]
        ask, ask_qty = asks[0]
        if ask < bid:
            return None
        mid = (bid + ask) / 2.0
        qty_sum = bid_qty + ask_qty
        microprice = (
            (ask * bid_qty + bid * ask_qty) / qty_sum
            if qty_sum > 0.0 else mid
        )
        return {
            "bids": bids, "asks": asks,
            "bid": bid, "ask": ask,
            "mid": mid, "microprice": microprice,
            "spread_bps": (
                (ask - bid) / mid * 10_000.0 if mid > 0.0 else 0.0
            ),
            "bid_queue_qty": sum(qty for _, qty in bids),
            "ask_queue_qty": sum(qty for _, qty in asks),
            "static_imbalance": (
                (sum(qty for _, qty in bids) - sum(qty for _, qty in asks))
                / max(
                    1e-12,
                    sum(qty for _, qty in bids) + sum(qty for _, qty in asks),
                )
            ),
        }

    @staticmethod
    def _response(before, after, side, observed_at_ms):
        direction = 1.0 if side == "LONG" else -1.0
        pre_mid = _f(before.get("mid"))
        pre_micro = _f(before.get("microprice"))
        opposite_key = "ask_queue_qty" if side == "LONG" else "bid_queue_qty"
        same_key = "bid_queue_qty" if side == "LONG" else "ask_queue_qty"
        pre_opposite = _f(before.get(opposite_key))
        pre_same = _f(before.get(same_key))
        return {
            "observed_at_ms": int(observed_at_ms),
            "signed_mid_response_bps": round(
                direction * (_f(after.get("mid")) - pre_mid)
                / pre_mid * 10_000.0 if pre_mid > 0.0 else 0.0,
                6,
            ),
            "signed_microprice_response_bps": round(
                direction * (_f(after.get("microprice")) - pre_micro)
                / pre_micro * 10_000.0 if pre_micro > 0.0 else 0.0,
                6,
            ),
            "opposite_queue_change_qty": round(
                _f(after.get(opposite_key)) - pre_opposite, 9
            ),
            "same_side_queue_change_qty": round(
                _f(after.get(same_key)) - pre_same, 9
            ),
            "spread_change_bps": round(
                _f(after.get("spread_bps")) - _f(before.get("spread_bps")),
                6,
            ),
        }

    def _emit(self, tracker, now_ms, valid=True, reason="COMPLETE"):
        responses = tracker["responses"]
        first_positive_ms = next((
            horizon for horizon in SPOT_HORIZONS_MS
            if _f((responses.get(horizon) or {}).get(
                "signed_microprice_response_bps"
            )) > 0.0
        ), None)
        pre_move_bps = _f(tracker.get("pre_impulse_signed_mid_move_bps"))
        if pre_move_bps > 0.0:
            causal_order = "FLOW_CHASES_PRICE"
        elif first_positive_ms in (50, 100):
            causal_order = "COINCIDENT"
        elif first_positive_ms in (250, 500):
            causal_order = "FLOW_LEADS_PRICE"
        else:
            causal_order = "NONCONVERSION"
        payload = {
            "schema_version": "WSTRADE_RECORDER_RESEARCH_V5_CAUSAL_PROOF_SEMANTICS",
            "version": self.version,
            "venue": self.venue,
            "authority": False,
            "eligible_for_live_gate": False,
            "valid": bool(valid),
            "reason": reason,
            "causal_episode_id": tracker["causal_episode_id"],
            "side": tracker["side"],
            "impulse_receive_time_ms": tracker["start_ms"],
            "impulse_event_time_ms": tracker.get("event_time_ms"),
            "corrected_event_time_ms": tracker.get("corrected_event_time_ms"),
            "clock_uncertainty_ms": tracker.get("clock_uncertainty_ms"),
            "clock_valid": tracker.get("clock_valid"),
            "freshness_time_basis": "RECEIVE_TIME",
            "causal_order_time_basis": (
                "CORRECTED_EVENT_TIME_WITH_UNCERTAINTY"
            ),
            "executed_buy_qty": tracker["buy_qty"],
            "executed_sell_qty": tracker["sell_qty"],
            "directional_imbalance": tracker["imbalance"],
            "pre_event": {
                key: round(value, 9) if isinstance(value, float) else value
                for key, value in tracker["before"].items()
                if key not in ("bids", "asks")
            },
            "responses": {
                str(horizon): responses.get(horizon)
                for horizon in SPOT_HORIZONS_MS
            },
            "pre_impulse_signed_mid_move_bps": round(pre_move_bps, 6),
            "pre_impulse_lookback_ms": tracker.get("pre_impulse_lookback_ms"),
            "first_positive_response_ms": first_positive_ms,
            "flow_price_causal_order": causal_order,
            "causal_order_policy": "SIGN_ORDER_RESEARCH_ONLY_NO_AUTHORITY",
            "static_imbalance_authority": False,
            "depth_without_executed_flow_authority": False,
            "mechanism_status": "RESEARCH_HYPOTHESIS_ONLY",
        }
        self.emit(self.output_stream, payload, event_time_ms=int(now_ms))
        if valid:
            self.completed += 1
        else:
            self.invalid += 1

    def reset(self, now_ms, reason="SPOT_DEPTH_EPOCH_RESET"):
        while self.pending:
            self._emit(self.pending.popleft(), now_ms, False, reason)
        self.book = None
        self.book_history.clear()

    def _pre_impulse_move(self, now_ms, side):
        rows = [
            (at_ms, snapshot) for at_ms, snapshot in self.book_history
            if 0 <= int(now_ms) - int(at_ms) <= 500
        ]
        if not rows or self.book is None:
            return 0.0, None
        at_ms, before = rows[0]
        old_mid = _f(before.get("mid"))
        current_mid = _f(self.book.get("mid"))
        direction = 1.0 if side == "LONG" else -1.0
        move = (
            direction * (current_mid - old_mid) / old_mid * 10_000.0
            if old_mid > 0.0 and current_mid > 0.0 else 0.0
        )
        return move, max(0, int(now_ms) - int(at_ms))

    def _advance(self, now_ms, snapshot=None):
        keep = deque(maxlen=self.pending.maxlen)
        completed = []
        for tracker in self.pending:
            elapsed = int(now_ms) - tracker["start_ms"]
            if snapshot is not None:
                for horizon in SPOT_HORIZONS_MS:
                    if elapsed >= horizon and horizon not in tracker["responses"]:
                        tracker["responses"][horizon] = self._response(
                            tracker["before"], snapshot, tracker["side"], now_ms
                        )
            if elapsed >= SPOT_HORIZONS_MS[-1]:
                complete = all(
                    horizon in tracker["responses"]
                    for horizon in SPOT_HORIZONS_MS
                )
                completed.append((tracker, complete))
            else:
                keep.append(tracker)
        self.pending = keep
        # Detach completed trackers before callbacks can emit another record.
        for tracker, complete in completed:
            self._emit(
                tracker, now_ms, complete,
                "COMPLETE" if complete else "DEPTH_RESPONSE_INCOMPLETE",
            )

    def observe(self, record):
        stream = str(record.get("stream") or "")
        if stream in (
            "spot_liquidity_response", "coinbase_liquidity_response",
            "liquidity_response",
        ):
            return
        now_ms = available_time_ms(record)
        if now_ms <= 0:
            return
        payload = record.get("payload") or {}
        if stream == self.depth_stream:
            snapshot = self._snapshot(payload)
            if snapshot is None:
                self.reset(now_ms, "INVALID_SPOT_DEPTH5")
                return
            self.book = snapshot
            self.book_history.append((now_ms, dict(snapshot)))
            self._advance(now_ms, snapshot)
            return

        if stream != self.trade_stream or self.book is None:
            return
        buy = _f(payload.get("buy_qty"))
        sell = _f(payload.get("sell_qty"))
        total = buy + sell
        if total <= 0.0:
            return
        imbalance = (buy - sell) / total
        if abs(imbalance) < 0.20:
            return
        side = "LONG" if imbalance > 0.0 else "SHORT"
        start_ms = now_ms
        pre_move_bps, pre_lookback_ms = self._pre_impulse_move(start_ms, side)
        first_id = payload.get("first_trade_id")
        last_id = payload.get("last_trade_id")
        self.pending.append({
            "causal_episode_id": (
                f"{self.venue}-depth:{side}:{start_ms}:{first_id}:{last_id}"
            ),
            "start_ms": start_ms,
            "event_time_ms": payload.get("last_event_time_ms"),
            "corrected_event_time_ms": payload.get("corrected_event_time_ms"),
            "clock_uncertainty_ms": payload.get("clock_uncertainty_ms"),
            "clock_valid": payload.get("clock_valid"),
            "side": side,
            "buy_qty": buy,
            "sell_qty": sell,
            "imbalance": round(imbalance, 6),
            "before": dict(self.book),
            "pre_impulse_signed_mid_move_bps": pre_move_bps,
            "pre_impulse_lookback_ms": pre_lookback_ms,
            "responses": {},
        })

    def summary(self):
        return {
            "version": self.version,
            "venue": self.venue,
            "authority": False,
            "completed": self.completed,
            "invalid": self.invalid,
            "pending": len(self.pending),
            "eligible_for_live_gate": False,
        }
