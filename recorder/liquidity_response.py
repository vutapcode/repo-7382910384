"""Offline-only executed depletion/refill research.

Static walls and disappearing levels are never treated as execution evidence.
Rows from this analyzer remain correlated observations until an ablation proves
incremental value beyond price and executed flow.
"""

from __future__ import annotations

from collections import deque

from recorder.depth import DepthGap, LocalOrderBook


VERSION = "LIQUIDITY_RESPONSE_OFFLINE_V1"
HORIZONS_MS = (250, 1_000, 3_000)


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
        depletion = sum(
            max(0.0, qty - after.get(price, 0.0))
            for price, qty in before.items()
        )
        executed = tracker["executed_qty"]
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

    def _update_pending(self, now_ms):
        keep = deque(maxlen=self.pending.maxlen)
        for tracker in self.pending:
            current = self._levels(self.book, tracker["side"], tracker["limit"])
            tracker["latest_levels"] = current
            pre_qty = self._qty(tracker["before_levels"])
            current_qty = self._qty(current)
            elapsed = now_ms - tracker["start_ms"]
            ratio = current_qty / pre_qty if pre_qty > 0.0 else 0.0
            for horizon in HORIZONS_MS:
                if elapsed >= horizon and horizon not in tracker["refill_ratio"]:
                    tracker["refill_ratio"][horizon] = round(ratio, 6)
            if (
                tracker.get("depletion_seen") and ratio >= 0.5
                and tracker.get("refill_half_life_ms") is None
            ):
                tracker["refill_half_life_ms"] = max(0, elapsed)
            if current_qty < pre_qty:
                tracker["depletion_seen"] = True
            if elapsed >= HORIZONS_MS[-1]:
                self._emit(tracker, now_ms)
            else:
                keep.append(tracker)
        self.pending = keep

    def observe(self, record):
        stream = str(record.get("stream") or "")
        if stream == "liquidity_response":
            return
        payload = record.get("payload") or {}
        now_ms = int(record.get("receive_time_ms", 0) or 0)
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
        self.pending.append({
            "start_ms": now_ms, "side": side, "executed_qty": executed,
            "limit": limit, "before_levels": before,
            "latest_levels": dict(before), "refill_ratio": {},
            "refill_half_life_ms": None, "depletion_seen": False,
        })

    def summary(self):
        return {
            "version": VERSION, "authority": False,
            "completed": self.completed, "invalid": self.invalid,
            "pending": len(self.pending), "eligible_for_live_gate": False,
        }
