"""Deterministic Coinbase Level-2 reconstruction for recorder research.

The book is data-only.  A quantity decrease is not called execution here;
executed Coinbase matches must be correlated by the liquidity analyzer before
any depletion hypothesis is emitted.
"""

import heapq


class CoinbaseL2Error(RuntimeError):
    pass


class CoinbaseL2Book:
    def __init__(self):
        self.bids = {}
        self.asks = {}
        self._top_bids = []
        self._top_asks = []
        self._cache_levels = 20
        self.synced = False
        self.epoch = 0

    @staticmethod
    def _side(rows):
        result = {}
        for row in rows or ():
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price, size = str(row[0]), str(row[1])
            try:
                price_value, size_value = float(price), float(size)
            except (TypeError, ValueError):
                continue
            if price_value > 0.0 and size_value > 0.0:
                result[price] = size
        return result

    def reset(self, snapshot):
        bids = self._side(snapshot.get("bids"))
        asks = self._side(snapshot.get("asks"))
        if not bids or not asks:
            raise CoinbaseL2Error("COINBASE_L2_EMPTY_SNAPSHOT")
        self.bids, self.asks = bids, asks
        self._refresh_top('buy')
        self._refresh_top('sell')
        self.epoch += 1
        self.synced = True
        self._validate()

    def apply(self, update):
        if not self.synced:
            raise CoinbaseL2Error("COINBASE_L2_UPDATE_WITHOUT_SNAPSHOT")
        refresh_bid = False
        refresh_ask = False
        for row in update.get("changes") or ():
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            side, price, size = str(row[0]).lower(), str(row[1]), str(row[2])
            if side not in ("buy", "sell"):
                continue
            try:
                price_value, size_value = float(price), float(size)
            except (TypeError, ValueError):
                continue
            if price_value <= 0.0 or size_value < 0.0:
                continue
            book_side = self.bids if side == "buy" else self.asks
            top = self._top_bids if side == "buy" else self._top_asks
            was_top = price in top
            existed = price in book_side
            if size_value == 0.0:
                book_side.pop(price, None)
                if was_top:
                    if side == "buy":
                        refresh_bid = True
                    else:
                        refresh_ask = True
            else:
                book_side[price] = size
                # Quantity-only changes cannot alter price rank.  A new level
                # only matters when it enters the cached top boundary.
                if not existed and (
                    len(top) < self._cache_levels
                    or not top
                    or (
                        side == "buy" and price_value > float(top[-1])
                    )
                    or (
                        side == "sell" and price_value < float(top[-1])
                    )
                ):
                    if side == "buy":
                        refresh_bid = True
                    else:
                        refresh_ask = True
        if refresh_bid:
            self._refresh_top('buy')
        if refresh_ask:
            self._refresh_top('sell')
        self._validate()

    def _refresh_top(self, side):
        if side == 'buy':
            self._top_bids = [price for price, _size in heapq.nlargest(
                self._cache_levels, self.bids.items(),
                key=lambda row: float(row[0]),
            )]
        else:
            self._top_asks = [price for price, _size in heapq.nsmallest(
                self._cache_levels, self.asks.items(),
                key=lambda row: float(row[0]),
            )]

    def _validate(self):
        if not self.bids or not self.asks:
            self.synced = False
            raise CoinbaseL2Error("COINBASE_L2_EMPTY_SIDE")
        if not self._top_bids:
            self._refresh_top('buy')
        if not self._top_asks:
            self._refresh_top('sell')
        if float(self._top_bids[0]) >= float(self._top_asks[0]):
            self.synced = False
            raise CoinbaseL2Error("COINBASE_L2_CROSSED_BOOK")

    def checkpoint(self, levels=20):
        levels = max(1, int(levels))
        if levels <= self._cache_levels:
            bids = [(price, self.bids[price]) for price in self._top_bids[:levels]]
            asks = [(price, self.asks[price]) for price in self._top_asks[:levels]]
        else:
            bids = heapq.nlargest(
                levels, self.bids.items(), key=lambda row: float(row[0])
            )
            asks = heapq.nsmallest(
                levels, self.asks.items(), key=lambda row: float(row[0])
            )
        return {
            "epoch": self.epoch,
            "bids": bids,
            "asks": asks,
        }


class CoinbaseL2UpdateBatcher:
    """Lossless ordered L2 transport with one recorder envelope per bucket."""

    def __init__(self, interval_ms=100):
        self.interval_ms = max(25, int(interval_ms))
        self._bucket_start_ms = None
        self._events = []

    def _start(self, receive_time_ms):
        self._bucket_start_ms = (
            int(receive_time_ms) // self.interval_ms * self.interval_ms
        )

    def push(self, receive_time_ms, event_time_ms, changes):
        receive_time_ms = int(receive_time_ms)
        bucket_start = receive_time_ms // self.interval_ms * self.interval_ms
        completed = None
        if self._bucket_start_ms is None:
            self._start(receive_time_ms)
        elif bucket_start != self._bucket_start_ms:
            completed = self.flush()
            self._start(receive_time_ms)
        clean_changes = [
            [str(row[0]), str(row[1]), str(row[2])]
            for row in changes or ()
            if isinstance(row, (list, tuple)) and len(row) >= 3
        ]
        self._events.append({
            "event_time_ms": int(event_time_ms),
            "receive_time_ms": receive_time_ms,
            "changes": clean_changes,
        })
        return completed

    def flush_due(self, now_ms):
        if self._bucket_start_ms is None:
            return None
        if int(now_ms) < self._bucket_start_ms + self.interval_ms:
            return None
        return self.flush()

    def flush(self):
        if self._bucket_start_ms is None or not self._events:
            self._bucket_start_ms = None
            self._events = []
            return None
        events = self._events
        payload = {
            "bucket_start_ms": self._bucket_start_ms,
            "bucket_end_ms": self._bucket_start_ms + self.interval_ms - 1,
            "first_event_time_ms": events[0]["event_time_ms"],
            "last_event_time_ms": events[-1]["event_time_ms"],
            "first_receive_time_ms": events[0]["receive_time_ms"],
            "last_receive_time_ms": events[-1]["receive_time_ms"],
            "update_count": len(events),
            "change_count": sum(len(row["changes"]) for row in events),
            "events": events,
        }
        self._bucket_start_ms = None
        self._events = []
        return payload
