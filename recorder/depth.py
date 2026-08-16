"""Deterministic USD-M Futures local order-book reconstruction."""


class DepthGap(RuntimeError):
    pass


class LocalOrderBook:
    def __init__(self):
        self.bids = {}
        self.asks = {}
        self.snapshot_update_id = None
        self.last_u = None
        self.synced = False

    def reset(self, snapshot):
        self.bids = {str(price): str(qty) for price, qty in snapshot.get('bids', [])}
        self.asks = {str(price): str(qty) for price, qty in snapshot.get('asks', [])}
        self.snapshot_update_id = int(snapshot['lastUpdateId'])
        self.last_u = self.snapshot_update_id
        self.synced = False

    def reset_checkpoint(self, checkpoint):
        """Restore recorder-owned state whose lastUpdateId is already applied."""
        self.bids = {
            str(price): str(qty) for price, qty in checkpoint.get('bids', [])
        }
        self.asks = {
            str(price): str(qty) for price, qty in checkpoint.get('asks', [])
        }
        last = int(checkpoint['lastUpdateId'])
        self.snapshot_update_id = int(checkpoint.get('snapshotUpdateId', last))
        self.last_u = last
        self.synced = True

    @staticmethod
    def _apply_side(side, updates):
        for price, quantity in updates:
            price = str(price)
            quantity = str(quantity)
            if float(quantity) == 0.0:
                side.pop(price, None)
            else:
                side[price] = quantity

    def apply(self, event):
        first_u = int(event['U'])
        final_u = int(event['u'])
        previous_u = int(event.get('pu', 0) or 0)
        if self.snapshot_update_id is None:
            raise DepthGap('DEPTH_WITHOUT_SNAPSHOT')

        if not self.synced:
            if final_u < self.snapshot_update_id:
                return 'STALE'
            if not first_u <= self.snapshot_update_id <= final_u:
                raise DepthGap(
                    f'INITIAL_RANGE_MISS snapshot={self.snapshot_update_id} '
                    f'U={first_u} u={final_u}'
                )
        elif previous_u != self.last_u:
            raise DepthGap(
                f'PU_GAP expected={self.last_u} actual={previous_u} '
                f'U={first_u} u={final_u}'
            )

        self._apply_side(self.bids, event.get('b', []))
        self._apply_side(self.asks, event.get('a', []))
        self.last_u = final_u
        self.synced = True
        return 'APPLIED'

    def checkpoint(self, levels=1000):
        bids = sorted(self.bids.items(), key=lambda row: float(row[0]), reverse=True)
        asks = sorted(self.asks.items(), key=lambda row: float(row[0]))
        return {
            'lastUpdateId': self.last_u,
            'snapshotUpdateId': self.snapshot_update_id,
            'bids': bids[:levels],
            'asks': asks[:levels],
        }

    def best_ticker(self):
        if not self.bids or not self.asks:
            return None
        bid_price = max(self.bids, key=float)
        ask_price = min(self.asks, key=float)
        return {
            'u': self.last_u,
            'b': bid_price,
            'B': self.bids[bid_price],
            'a': ask_price,
            'A': self.asks[ask_price],
            'derived_from_depth': True,
        }

    def microstructure(self, bands_bps=(5, 10, 25)):
        """One O(depth) snapshot per second; never called on every diff."""
        ticker = self.best_ticker()
        if ticker is None:
            return None
        bid = float(ticker['b'])
        ask = float(ticker['a'])
        bid_qty = float(ticker['B'])
        ask_qty = float(ticker['A'])
        mid = (bid + ask) / 2.0
        microprice = (
            (ask * bid_qty + bid * ask_qty) / (bid_qty + ask_qty)
            if bid_qty + ask_qty > 0.0 else mid
        )
        result = {
            'last_u': self.last_u,
            'best_bid': bid,
            'best_ask': ask,
            'mid': mid,
            'spread_bps': (ask - bid) / mid * 10000.0 if mid > 0.0 else None,
            'microprice': microprice,
            'microprice_offset_bps': (
                (microprice - mid) / mid * 10000.0 if mid > 0.0 else None
            ),
            'top_bid_qty': bid_qty,
            'top_ask_qty': ask_qty,
            'top_imbalance': (
                (bid_qty - ask_qty) / (bid_qty + ask_qty)
                if bid_qty + ask_qty > 0.0 else 0.0
            ),
        }
        bid_levels = [(float(price), float(qty)) for price, qty in self.bids.items()]
        ask_levels = [(float(price), float(qty)) for price, qty in self.asks.items()]
        for band in bands_bps:
            lower = mid * (1.0 - band / 10000.0)
            upper = mid * (1.0 + band / 10000.0)
            bid_depth = sum(qty for price, qty in bid_levels if price >= lower)
            ask_depth = sum(qty for price, qty in ask_levels if price <= upper)
            total = bid_depth + ask_depth
            result[f'bid_depth_{band}bps'] = bid_depth
            result[f'ask_depth_{band}bps'] = ask_depth
            result[f'obi_{band}bps'] = (
                (bid_depth - ask_depth) / total if total > 0.0 else 0.0
            )
        return result
