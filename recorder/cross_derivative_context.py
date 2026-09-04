"""Authority-free Binance/Bybit positioning relation for offline research."""

from collections import deque


VERSION = "CROSS_DERIVATIVE_CONTEXT_V1"
AUTHORITY = False


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class CrossDerivativeContext:
    """Record factual OI co-movement without manufacturing trade direction."""

    def __init__(self, emit):
        self.emit = emit
        self.oi = {
            "binance_usdm": deque(maxlen=8),
            "bybit_linear": deque(maxlen=8),
        }
        self.liquidations = deque(maxlen=256)
        self.last_identity = None
        self.emitted = 0

    @staticmethod
    def _time(record):
        return int(
            record.get("available_time_ms")
            or record.get("receive_time_ms")
            or record.get("event_time_ms") or 0
        )

    def _observe_oi(self, record, venue):
        payload = dict(record.get("payload") or {})
        value = _f(
            payload.get("open_interest", payload.get("openInterest"))
        )
        observed_ms = self._time(record)
        epoch = int(record.get("epoch", 0) or 0)
        if value is None or value <= 0.0 or observed_ms <= 0:
            return False
        rows = self.oi[venue]
        if rows and observed_ms <= rows[-1][0]:
            return False
        rows.append((observed_ms, epoch, value))
        return True

    def _observe_liquidation(self, record):
        payload = dict(record.get("payload") or {})
        observed_ms = self._time(record)
        size = _f(payload.get("executed_size"), 0.0) or 0.0
        side = str(payload.get("liquidated_position_side") or "UNKNOWN")
        if observed_ms > 0 and size > 0.0:
            self.liquidations.append((observed_ms, side, size))

    @staticmethod
    def _change(rows):
        if len(rows) < 2:
            return None
        before, after = rows[-2], rows[-1]
        if before[1] != after[1] or before[2] <= 0.0:
            return None
        return {
            "before_available_ms": before[0],
            "after_available_ms": after[0],
            "epoch": after[1],
            "before": before[2],
            "after": after[2],
            "delta_pct": (after[2] - before[2]) / before[2] * 100.0,
        }

    def _snapshot(self, now_ms):
        binance = self._change(self.oi["binance_usdm"])
        bybit = self._change(self.oi["bybit_linear"])
        if binance is None or bybit is None:
            return None
        b_sign = (binance["delta_pct"] > 0) - (binance["delta_pct"] < 0)
        y_sign = (bybit["delta_pct"] > 0) - (bybit["delta_pct"] < 0)
        if b_sign > 0 and y_sign > 0:
            relation = "BOTH_EXPANDING"
        elif b_sign < 0 and y_sign < 0:
            relation = "BOTH_CONTRACTING"
        elif b_sign and y_sign and b_sign != y_sign:
            relation = "DIVERGENT"
        else:
            relation = "UNCHANGED_OR_INSUFFICIENT"
        recent = [row for row in self.liquidations if 0 <= now_ms - row[0] <= 15_000]
        if relation == "BOTH_CONTRACTING" and recent:
            mechanism = "CROSS_DERIVATIVE_FORCED_UNWIND_CANDIDATE"
        elif relation == "BOTH_CONTRACTING":
            mechanism = "POSITIONING_CONTRACTION_ONLY"
        elif relation == "BOTH_EXPANDING":
            mechanism = "POSITIONING_EXPANSION_ONLY"
        elif relation == "DIVERGENT":
            mechanism = "VENUE_LOCAL_OR_ASYNCHRONOUS"
        else:
            mechanism = "UNRESOLVED"
        return {
            "version": VERSION,
            "authority": AUTHORITY,
            "direction_authority": False,
            "veto_authority": False,
            "semantic_role": "DERIVATIVE_MECHANISM_RESEARCH_ONLY",
            "relation": relation,
            "mechanism_hypothesis": mechanism,
            "binance_oi": binance,
            "bybit_oi": bybit,
            "availability_skew_ms": abs(
                binance["after_available_ms"] - bybit["after_available_ms"]
            ),
            "recent_bybit_liquidations": {
                "count": len(recent),
                "long_size": sum(row[2] for row in recent if row[1] == "LONG"),
                "short_size": sum(row[2] for row in recent if row[1] == "SHORT"),
            },
            "interpretation_limit": (
                "POSITIONING_RELATION_ONLY_NOT_CASH_DIRECTION_OR_ENTRY_VETO"
            ),
        }

    def observe(self, record):
        stream = str(record.get("stream") or "")
        source = str(record.get("source") or "")
        changed = False
        if stream == "open_interest" and source == "binance_usdm":
            changed = self._observe_oi(record, "binance_usdm")
        elif stream == "bybit_derivative_state":
            changed = self._observe_oi(record, "bybit_linear")
        elif stream == "bybit_liquidation":
            self._observe_liquidation(record)
            return
        else:
            return
        if not changed:
            return
        now_ms = self._time(record)
        snapshot = self._snapshot(now_ms)
        if snapshot is None:
            return
        identity = (
            snapshot["binance_oi"]["after_available_ms"],
            snapshot["bybit_oi"]["after_available_ms"],
        )
        if identity == self.last_identity:
            return
        self.last_identity = identity
        self.emit("cross_derivative_context", snapshot, event_time_ms=now_ms)
        self.emitted += 1

    def summary(self):
        return {
            "version": VERSION,
            "authority": False,
            "emitted": self.emitted,
            "binance_samples": len(self.oi["binance_usdm"]),
            "bybit_samples": len(self.oi["bybit_linear"]),
        }
