"""Deterministic Coinbase Level-2 reconstruction for recorder research.

The book is data-only.  A quantity decrease is not called execution here;
executed Coinbase matches must be correlated by the liquidity analyzer before
any depletion hypothesis is emitted.
"""


class CoinbaseL2Error(RuntimeError):
    pass


class CoinbaseL2Book:
    def __init__(self):
        self.bids = {}
        self.asks = {}
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
        self.epoch += 1
        self.synced = True
        self._validate()

    def apply(self, update):
        if not self.synced:
            raise CoinbaseL2Error("COINBASE_L2_UPDATE_WITHOUT_SNAPSHOT")
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
            if size_value == 0.0:
                book_side.pop(price, None)
            else:
                book_side[price] = size
        self._validate()

    def _validate(self):
        if not self.bids or not self.asks:
            self.synced = False
            raise CoinbaseL2Error("COINBASE_L2_EMPTY_SIDE")
        if max(map(float, self.bids)) >= min(map(float, self.asks)):
            self.synced = False
            raise CoinbaseL2Error("COINBASE_L2_CROSSED_BOOK")

    def checkpoint(self, levels=20):
        levels = max(1, int(levels))
        bids = sorted(self.bids.items(), key=lambda row: float(row[0]), reverse=True)
        asks = sorted(self.asks.items(), key=lambda row: float(row[0]))
        return {
            "epoch": self.epoch,
            "bids": bids[:levels],
            "asks": asks[:levels],
        }

