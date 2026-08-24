"""[AI_CONTEXT] RETIRED EXPERIMENT: no live trading authority.

This file is not imported by `mainnet_tier_s_lean_launcher.py` or its active
launcher chain. It may be used only for isolated research. Do not wire its
CATCH/SHADOW_PROBE output into Bias, Entry, Guardian, Risk, promotion or live
execution. File existence is not evidence that a module is active.
"""

from collections import deque
from dataclasses import asdict, dataclass
import math
import time


VERSION = "WHALE_INTENT_V1"
WINDOWS = (0.25, 1.0, 3.0, 15.0, 60.0)
MIN_FLOW_VOLUME_BTC = 0.02
MAX_FLOW_VOLUME_FLOOR_BTC = 0.10


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def _mid(bid, ask):
    bid, ask = float(bid or 0.0), float(ask or 0.0)
    return (bid + ask) / 2.0 if bid > 0.0 and ask > bid else max(bid, ask)


def _sg(side):
    return 1.0 if side == "LONG" else -1.0


@dataclass(frozen=True)
class WhaleIntentSnapshot:
    version: str
    state: str
    side: str
    confidence: float
    lane: str
    evidence: tuple
    vetoes: tuple
    flow: dict
    price_moves_bps: dict
    depth: dict
    oi_change_pct: float
    liquidation_quote: dict
    feed_epochs: dict
    flow_volume_floor_btc: float
    opportunity_id: int
    ts: float

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class TradeIntent:
    lane: str
    side: str
    entry_style: str
    confidence: float
    evidence: tuple
    vetoes: tuple
    invalidation_max_pct: float
    ts: float

    def to_dict(self):
        return asdict(self)


class RollingFlow:
    def __init__(self, windows=WINDOWS):
        self.windows = tuple(windows)
        self.rows = {window: deque() for window in self.windows}
        self.buy = {window: 0.0 for window in self.windows}
        self.sell = {window: 0.0 for window in self.windows}

    def clear(self):
        for window in self.windows:
            self.rows[window].clear()
            self.buy[window] = self.sell[window] = 0.0

    def push(self, now, buy, sell):
        buy, sell = max(0.0, float(buy)), max(0.0, float(sell))
        for window in self.windows:
            rows = self.rows[window]
            rows.append((now, buy, sell))
            self.buy[window] += buy
            self.sell[window] += sell
            cutoff = now - window
            while rows and rows[0][0] < cutoff:
                _, old_buy, old_sell = rows.popleft()
                self.buy[window] -= old_buy
                self.sell[window] -= old_sell

    def snap(self, window):
        buy = max(0.0, self.buy[window])
        sell = max(0.0, self.sell[window])
        total = buy + sell
        return {
            "buy": buy, "sell": sell, "volume": total,
            "imbalance": (buy - sell) / total if total > 0.0 else 0.0,
        }


class RollingScalar:
    def __init__(self, windows=WINDOWS):
        self.windows = tuple(windows)
        self.rows = {window: deque() for window in self.windows}

    def push(self, now, value):
        value = float(value or 0.0)
        if value <= 0.0:
            return
        for window in self.windows:
            rows = self.rows[window]
            rows.append((now, value))
            cutoff = now - window
            while len(rows) > 1 and rows[1][0] <= cutoff:
                rows.popleft()

    def change_bps(self, window):
        rows = self.rows[window]
        if len(rows) < 2 or rows[0][1] <= 0.0:
            return 0.0
        return (rows[-1][1] - rows[0][1]) / rows[0][1] * 10000.0

    def change_pct(self, window):
        return self.change_bps(window) / 100.0


class WhaleIntentEngine:
    def __init__(self):
        self.flows = {name: RollingFlow() for name in ("spot", "coinbase", "futures")}
        self.prices = {name: RollingScalar() for name in ("spot", "coinbase", "futures")}
        self.oi = RollingScalar()
        self.liquidations = {"long": RollingFlow(), "short": RollingFlow()}
        self.previous_counters = {}
        self.previous_state = "INVALID"
        self.previous_side = "ABSTAIN"
        self.last_sample_at = 0.0
        self.pending_opportunities = deque(maxlen=256)
        self.qualified_opportunities = 0
        self.captured_opportunities = 0

    def mark_captured(self, state, side, now=None):
        now = time.time() if now is None else float(now)
        for row in reversed(self.pending_opportunities):
            if row["side"] == side and now - row["started_at"] <= 60.0:
                row["captured"] = True
                row["captured_at"] = now
                break
        state.whale_opportunity_captured_pending = True

    @staticmethod
    def claim_opportunity(snapshot, state):
        """Consume one causal whale episode at most once, including restarts."""
        opportunity_id = int((snapshot or {}).get("opportunity_id", 0) or 0)
        consumed = int(
            getattr(state, "whale_last_consumed_opportunity_id", 0) or 0
        )
        if opportunity_id <= 0 or opportunity_id <= consumed:
            return False
        state.whale_last_consumed_opportunity_id = opportunity_id
        return True

    @staticmethod
    def adopt_position_opportunity(snapshot, state, position):
        """Bind an ongoing same-side episode to the position already owning it.

        The engine's in-memory episode state starts cold after a process restart.
        Its first active snapshot can therefore receive a new monotonic id even
        though the market move is still supporting the restored position.  Mark
        that id consumed so the same release cannot immediately re-enter after
        Guardian closes the position.  Opposite-side episodes remain available
        as genuinely new reversal opportunities.
        """
        if position is None or not bool(getattr(position, "active", False)):
            return False
        snapshot = snapshot or {}
        if str(snapshot.get("state", "")).upper() not in (
            "LOADING", "PRESSURE", "RELEASE", "SUPPORT",
        ):
            return False
        if str(snapshot.get("side", "")).upper() != str(
            getattr(position, "side", "")
        ).upper():
            return False
        opportunity_id = int(snapshot.get("opportunity_id", 0) or 0)
        consumed = int(
            getattr(state, "whale_last_consumed_opportunity_id", 0) or 0
        )
        if opportunity_id <= 0 or opportunity_id <= consumed:
            return False
        state.whale_last_consumed_opportunity_id = opportunity_id
        position.whale_opportunity_id = opportunity_id
        return True

    def _update_outcomes(self, state, now, spot_price):
        # Runtime state persists only completed opportunity counters. Pending
        # 60-second outcomes are intentionally discarded on restart, while
        # completed evidence remains monotonic across the 72-hour soak.
        self.qualified_opportunities = max(
            self.qualified_opportunities,
            int(getattr(state, "whale_opportunity_qualified", 0) or 0),
        )
        self.captured_opportunities = max(
            self.captured_opportunities,
            int(getattr(state, "whale_opportunity_captured", 0) or 0),
        )
        if spot_price <= 0.0:
            return
        kept = deque(maxlen=256)
        for row in self.pending_opportunities:
            signed_bps = _sg(row["side"]) * (
                spot_price - row["price"]
            ) / row["price"] * 10000.0
            row["mfe_bps"] = max(float(row.get("mfe_bps", 0.0)), signed_bps)
            row["mae_bps"] = min(float(row.get("mae_bps", 0.0)), signed_bps)
            if now - row["started_at"] < 60.0:
                kept.append(row)
                continue
            qualified = row["mfe_bps"] >= 25.0 and row["mae_bps"] >= -15.0
            if qualified:
                self.qualified_opportunities += 1
                self.captured_opportunities += int(bool(row.get("captured")))
        self.pending_opportunities = kept
        state.whale_opportunity_qualified = self.qualified_opportunities
        state.whale_opportunity_captured = self.captured_opportunities
        state.whale_opportunity_recall = (
            self.captured_opportunities / self.qualified_opportunities
            if self.qualified_opportunities else None
        )

    def _counter_delta(self, key, current):
        current = max(0.0, float(current or 0.0))
        previous = self.previous_counters.get(key)
        self.previous_counters[key] = current
        if previous is None or current < previous:
            return 0.0
        return current - previous

    def _sample(self, state, now):
        counters = {
            "spot_buy": getattr(state, "spot_cvd_buy_total", getattr(state, "cvd_buy", 0.0)),
            "spot_sell": getattr(state, "spot_cvd_sell_total", getattr(state, "cvd_sell", 0.0)),
            "coinbase_buy": getattr(state, "coinbase_cvd_buy_total", 0.0),
            "coinbase_sell": getattr(state, "coinbase_cvd_sell_total", 0.0),
            "futures_buy": getattr(state, "futures_cvd_buy_total", 0.0),
            "futures_sell": getattr(state, "futures_cvd_sell_total", 0.0),
        }
        for venue in ("spot", "coinbase", "futures"):
            self.flows[venue].push(
                now,
                self._counter_delta(f"{venue}_buy", counters[f"{venue}_buy"]),
                self._counter_delta(f"{venue}_sell", counters[f"{venue}_sell"]),
            )
        long_quote = self._counter_delta(
            "long_liq", getattr(state, "long_liquidation_quote_total", 0.0)
        )
        short_quote = self._counter_delta(
            "short_liq", getattr(state, "short_liquidation_quote_total", 0.0)
        )
        self.liquidations["long"].push(now, long_quote, 0.0)
        self.liquidations["short"].push(now, short_quote, 0.0)

        self.prices["spot"].push(now, _mid(
            getattr(state, "best_bid", 0.0), getattr(state, "best_ask", 0.0)
        ))
        self.prices["coinbase"].push(now, getattr(state, "coinbase_price", 0.0))
        self.prices["futures"].push(now, _mid(
            getattr(state, "execution_best_bid", 0.0),
            getattr(state, "execution_best_ask", 0.0),
        ))
        self.oi.push(now, getattr(state, "open_interest", 0.0))

    def _fresh(self, state, now):
        checks = {
            "spot": (getattr(state, "thoi_gian_tick_cuoi", 0.0), 3.0),
            "coinbase": (getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0), 5.0),
            "futures": (getattr(state, "execution_price_time", 0.0), 3.0),
            "spot_flow": (getattr(state, "thoi_gian_dong_tien_cuoi", 0.0), 5.0),
            "futures_flow": (getattr(state, "thoi_gian_dong_tien_futures_cuoi", 0.0), 5.0),
            "depth": (getattr(state, "futures_depth_updated_at", 0.0), 2.0),
            "oi": (getattr(state, "thoi_gian_vi_mo_cuoi", 0.0), 20.0),
        }
        return {
            name: bool(float(stamp or 0.0) > 0.0 and 0.0 <= now - float(stamp) <= age)
            for name, (stamp, age) in checks.items()
        }

    def evaluate(self, state, now=None):
        now = time.time() if now is None else float(now)
        if now - self.last_sample_at >= 0.05:
            self._sample(state, now)
            self.last_sample_at = now
        fresh = self._fresh(state, now)
        spot_price = _mid(getattr(state, "best_bid", 0.0), getattr(state, "best_ask", 0.0))
        self._update_outcomes(state, now, spot_price)
        critical = all(fresh[name] for name in (
            "spot", "coinbase", "futures", "spot_flow", "futures_flow", "depth"
        )) and bool(getattr(state, "futures_depth_synced", False))

        flow = {
            venue: {str(window): self.flows[venue].snap(window) for window in WINDOWS}
            for venue in self.flows
        }
        moves = {
            venue: {str(window): self.prices[venue].change_bps(window) for window in WINDOWS}
            for venue in self.prices
        }
        cash_score = sum(
            flow[venue]["1.0"]["imbalance"] + 0.5 * flow[venue]["3.0"]["imbalance"]
            for venue in ("spot", "coinbase")
        )
        price_score = sum(moves[venue]["1.0"] for venue in ("spot", "coinbase"))
        directional = cash_score + _clamp(price_score / 2.0, -1.0, 1.0)
        side = "LONG" if directional > 0.08 else "SHORT" if directional < -0.08 else "ABSTAIN"
        sign = _sg(side) if side != "ABSTAIN" else 0.0

        volume_floor = max(
            MIN_FLOW_VOLUME_BTC,
            min(
                MAX_FLOW_VOLUME_FLOOR_BTC,
                0.02 * float(getattr(state, "vol_pct90", 0.0) or 0.0),
            ),
        )
        flow_supporters = []
        strong_flow = []
        signed_flow = {}
        price_supporters = []
        signed_moves = {}
        if side != "ABSTAIN":
            for venue in ("spot", "coinbase", "futures"):
                row = flow[venue]["1.0"]
                value = row["imbalance"] * sign
                signed_flow[venue] = value
                volume_ok = float(row.get("volume", 0.0) or 0.0) >= volume_floor
                if volume_ok and value >= 0.08:
                    flow_supporters.append(venue)
                if volume_ok and value >= 0.18:
                    strong_flow.append(venue)
                move = moves[venue]["1.0"] * sign
                signed_moves[venue] = move
                if move >= 0.40:
                    price_supporters.append(venue)

        cash_anchor = bool(
            side != "ABSTAIN"
            and (
                all(name in flow_supporters for name in ("spot", "coinbase"))
                or (
                    "spot" in flow_supporters
                    and all(signed_moves.get(name, 0.0) >= 0.20 for name in ("spot", "coinbase"))
                )
            )
        )
        executed_flow = bool(cash_anchor and len(flow_supporters) >= 2)
        price_response = bool(
            len([name for name in ("spot", "coinbase") if name in price_supporters]) >= 1
            and "futures" in price_supporters
        )

        depth = dict(getattr(state, "futures_depth_metrics", {}) or {})
        imbalance = float(depth.get("imbalance_top20", 0.0) or 0.0) * sign
        consumed = (
            float(depth.get("ask_removed", 0.0) or 0.0)
            if side == "LONG" else float(depth.get("bid_removed", 0.0) or 0.0)
        )
        replenished = (
            float(depth.get("bid_replenished", 0.0) or 0.0)
            if side == "LONG" else float(depth.get("ask_replenished", 0.0) or 0.0)
        )
        futures_exec_250ms = float(
            flow["futures"]["0.25"]["buy" if side == "LONG" else "sell"] or 0.0
        )
        removal_has_fills = bool(
            consumed <= 0.05
            or futures_exec_250ms >= max(0.01, 0.15 * consumed)
        )
        depth_confirmed = bool(
            side != "ABSTAIN" and imbalance >= 0.08 and consumed > 0.0
            and "futures" in flow_supporters and removal_has_fills
        )
        cash_best = max(
            (signed_moves.get("spot", 0.0), signed_moves.get("coinbase", 0.0)),
            default=0.0,
        )
        perp_move = signed_moves.get("futures", 0.0)
        perp_trap = bool(side != "ABSTAIN" and perp_move >= 1.25 and cash_best < 0.40)
        flow_strength = max((signed_flow.get(name, 0.0) for name in flow_supporters), default=0.0)
        absorption = bool(
            executed_flow and flow_strength >= 0.18 and cash_best < 0.28
            and replenished > 0.0
        )
        oi_change = self.oi.change_pct(15.0) if fresh.get("oi") else 0.0
        oi_consistent = bool(side != "ABSTAIN" and oi_change >= 0.015 and price_response)
        liquidation_quote = {
            "long_15s": self.liquidations["long"].snap(15.0)["buy"],
            "short_15s": self.liquidations["short"].snap(15.0)["buy"],
        }
        liquidation_support = bool(
            side == "LONG" and liquidation_quote["short_15s"] >= 25_000.0
            or side == "SHORT" and liquidation_quote["long_15s"] >= 25_000.0
        )

        evidence = []
        for name, passed in (
            ("CASH_ANCHOR", cash_anchor), ("EXECUTED_FLOW", executed_flow),
            ("PRICE_RESPONSE", price_response), ("DEPTH_CONSUMPTION", depth_confirmed),
            ("OI_BUILD", oi_consistent), ("LIQUIDATION_RELEASE", liquidation_support),
            ("ABSORPTION", absorption),
        ):
            if passed:
                evidence.append(name)
        vetoes = []
        if not critical:
            vetoes.append("FEED_INVALID")
        if perp_trap:
            vetoes.append("PERP_TRAP")
        if side != "ABSTAIN" and consumed > 0.05 and not removal_has_fills:
            vetoes.append("WALL_WITHDRAWAL_WITHOUT_EXECUTED_FLOW")
        if bool(getattr(state, "futures_depth_gap_count", 0)) and not bool(
            getattr(state, "futures_depth_synced", False)
        ):
            vetoes.append("DEPTH_SEQUENCE_GAP")

        core_count = sum(name in evidence for name in (
            "CASH_ANCHOR", "EXECUTED_FLOW", "PRICE_RESPONSE", "DEPTH_CONSUMPTION"
        ))
        confidence = _clamp(
            0.18 * core_count + 0.08 * sum(
                name in evidence for name in ("OI_BUILD", "LIQUIDATION_RELEASE")
            ) + 0.08 * min(1.0, flow_strength)
        )
        prior_same_side = self.previous_side == side
        opposing_flow = bool(side != "ABSTAIN" and sum(
            value <= -0.08 for value in signed_flow.values()
        ) >= 2)
        adverse_price = bool(side != "ABSTAIN" and sum(
            value <= -0.40 for value in signed_moves.values()
        ) >= 2)
        prior_sign = _sg(self.previous_side) if self.previous_side in ("LONG", "SHORT") else 0.0
        prior_opposing_flow = bool(prior_sign and sum(
            flow[name]["1.0"]["imbalance"] * prior_sign <= -0.08
            for name in ("spot", "coinbase", "futures")
        ) >= 2)
        prior_adverse_price = bool(prior_sign and sum(
            moves[name]["1.0"] * prior_sign <= -0.40
            for name in ("spot", "coinbase", "futures")
        ) >= 2)
        whale_exhausted = bool(
            self.previous_state in ("RELEASE", "SUPPORT")
            and prior_opposing_flow and prior_adverse_price
        )

        feed_invalid = any(name in vetoes for name in (
            "FEED_INVALID", "DEPTH_SEQUENCE_GAP"
        ))
        if feed_invalid:
            intent_state = "INVALID"
        elif whale_exhausted:
            intent_state = "EXHAUSTION"
            side = self.previous_side
        elif vetoes or side == "ABSTAIN":
            intent_state = "TRAP" if "PERP_TRAP" in vetoes else "INVALID"
        # RELEASE means price/flow agreement plus book consumption backed by
        # executed Futures volume.  Without that final hand-off the intent is
        # still PRESSURE, not a taker-entry event.
        elif (
            cash_anchor and executed_flow and price_response
            and depth_confirmed and core_count >= 4
        ):
            intent_state = "RELEASE"
        elif prior_same_side and self.previous_state in ("RELEASE", "SUPPORT") and executed_flow:
            intent_state = "SUPPORT"
        elif executed_flow and (price_response or oi_consistent) and core_count >= 3:
            intent_state = "PRESSURE"
        elif absorption or (cash_anchor and depth_confirmed):
            intent_state = "LOADING"
        else:
            intent_state = "INVALID"

        lane = (
            "CATCH" if intent_state == "RELEASE" and confidence >= 0.68
            else "SHADOW_PROBE" if intent_state in ("LOADING", "PRESSURE")
            else "NONE"
        )
        active_states = ("LOADING", "PRESSURE", "RELEASE", "SUPPORT")
        if intent_state in active_states and (
            self.previous_state not in active_states or side != self.previous_side
        ):
            state.whale_opportunity_count = int(
                getattr(state, "whale_opportunity_count", 0) or 0
            ) + 1
            if spot_price > 0.0 and side in ("LONG", "SHORT"):
                self.pending_opportunities.append({
                    "started_at": now, "price": spot_price, "side": side,
                    "mfe_bps": 0.0, "mae_bps": 0.0, "captured": False,
                    "source_state": intent_state,
                })
        self.previous_state, self.previous_side = intent_state, side
        snapshot = WhaleIntentSnapshot(
            version=VERSION, state=intent_state, side=side,
            confidence=round(confidence, 6), lane=lane,
            evidence=tuple(evidence), vetoes=tuple(vetoes), flow=flow,
            price_moves_bps=moves, depth=depth, oi_change_pct=round(oi_change, 6),
            liquidation_quote=liquidation_quote,
            feed_epochs={
                "coinbase": int(getattr(state, "coinbase_flow_epoch", 0) or 0),
                "depth": int(getattr(state, "futures_depth_epoch", 0) or 0),
            }, flow_volume_floor_btc=round(volume_floor, 8),
            opportunity_id=int(getattr(state, "whale_opportunity_count", 0) or 0),
            ts=now,
        )
        state.whale_intent_snapshot = snapshot.to_dict()
        state.whale_intent_state = intent_state
        state.whale_intent_side = side
        state.whale_intent_confidence = confidence
        state.whale_intent_lane = lane
        return snapshot.to_dict()

    @staticmethod
    def trade_intent(snapshot):
        if not snapshot or snapshot.get("lane") != "CATCH":
            return None
        entry_style = "TAKER_RELEASE" if snapshot.get("state") == "RELEASE" else "MAKER_LOADING"
        return TradeIntent(
            lane="CATCH", side=snapshot["side"], entry_style=entry_style,
            confidence=float(snapshot.get("confidence", 0.0) or 0.0),
            evidence=tuple(snapshot.get("evidence") or ()),
            vetoes=tuple(snapshot.get("vetoes") or ()),
            invalidation_max_pct=0.55, ts=float(snapshot.get("ts", 0.0) or 0.0),
        ).to_dict()

    @staticmethod
    def entry_result(snapshot):
        intent = WhaleIntentEngine.trade_intent(snapshot)
        if intent is None or intent["vetoes"]:
            return None
        side = intent["side"]
        sign = _sg(side)
        moves = {
            venue: float((snapshot["price_moves_bps"].get(venue) or {}).get("1.0", 0.0)) * sign
            for venue in ("spot", "coinbase", "futures")
        }
        venues = {}
        supporters, strong = [], []
        for venue in ("spot", "coinbase", "futures"):
            row = (snapshot["flow"].get(venue) or {}).get("1.0") or {}
            signed = float(row.get("imbalance", 0.0) or 0.0) * sign
            venues[venue] = {
                "signed_imbalance": signed,
                "volume_btc": float(row.get("volume", 0.0) or 0.0),
            }
            if signed >= 0.08:
                supporters.append(venue)
            if signed >= 0.18:
                strong.append(venue)
        price_supporters = [name for name, value in moves.items() if value >= 0.40]
        return {
            "version": VERSION,
            "decision": "GO",
            "side": side,
            "entry_mode": "NORMAL",
            "phase": "RELEASE",
            "confidence": intent["confidence"],
            "bias_confidence": intent["confidence"],
            "price_threshold_bps": 0.40,
            "reason": "WHALE_RELEASE_CATCH",
            "lane": "CATCH",
            "entry_style": intent["entry_style"],
            "whale_intent": snapshot,
            "s_votes": {
                "S1_cross_venue_price_acceptance": {
                    "status": "PASS", "confidence": intent["confidence"],
                    "metrics": {"moves": moves, "supporters": price_supporters,
                                "strong_supporters": price_supporters},
                },
                "S2_multi_venue_executed_flow": {
                    "status": "PASS", "confidence": intent["confidence"],
                    "metrics": {
                                "ts": snapshot.get("ts"),
                                "volume_floor_btc": snapshot.get(
                                    "flow_volume_floor_btc", MIN_FLOW_VOLUME_BTC
                                ),
                                "venues": venues, "supporters": supporters,
                                "strong_supporters": strong, "strong_opponents": []},
                },
                "S3_price_x_oi_validator": {
                    "status": "PASS" if snapshot.get("oi_change_pct", 0.0) >= 0.015 else "NEUTRAL",
                    "confidence": 0.5,
                    "metrics": {"oi_change_pct": snapshot.get("oi_change_pct", 0.0)},
                },
            },
            "ts": snapshot["ts"],
        }
