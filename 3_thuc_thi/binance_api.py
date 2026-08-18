"""Async Binance USD-M Futures MAINNET REST/Algo adapter."""

import asyncio
import logging
import time

from binance.error import ClientError
from binance.um_futures import UMFutures


class BinanceAPI:
    BASE_URL = "https://fapi.binance.com"

    def __init__(self, api_key, secret_key):
        self.base_url = self.BASE_URL
        self.client = UMFutures(
            key=api_key,
            secret=secret_key,
            base_url=self.base_url,
            timeout=10,
        )

    @staticmethod
    def _error_payload(exc):
        if isinstance(exc, ClientError):
            return {"code": exc.error_code, "message": exc.error_message}, exc.status_code
        return {"code": "NETWORK", "message": str(exc)}, 599

    async def _call(self, func, *args, **kwargs):
        def invoke():
            try:
                return func(*args, **kwargs), 200
            except Exception as exc:
                return self._error_payload(exc)
        return await asyncio.to_thread(invoke)

    @staticmethod
    def _page_error(message):
        return {"code": "PAGINATION", "message": message}, 599

    async def get_balance(self):
        result, status = await self._call(self.client.balance)
        if status != 200:
            logging.error("[API] balance failed: %s", result)
            return 0.0
        for item in result:
            if item.get("asset") == "USDT":
                return float(item.get("balance", item.get("availableBalance", 0.0)))
        return 0.0

    async def get_balance_details(self):
        result, status = await self._call(self.client.balance)
        if status != 200:
            return {}, status
        row = next((item for item in result if item.get("asset") == "USDT"), {})
        return row, status

    async def get_position_mode(self):
        result, status = await self._call(self.client.get_position_mode)
        if status != 200:
            return None
        return bool(result.get("dualSidePosition"))

    async def change_position_mode(self, dual_side=True):
        return await self._call(
            self.client.change_position_mode,
            dualSidePosition="true" if dual_side else "false",
        )

    async def get_multi_asset_mode(self):
        return await self._call(self.client.get_multi_asset_mode)

    async def change_multi_asset_mode(self, enabled=False):
        return await self._call(
            self.client.change_multi_asset_mode,
            multiAssetsMargin="true" if enabled else "false",
        )

    async def change_margin_type(self, symbol, margin_type="ISOLATED"):
        return await self._call(
            self.client.change_margin_type,
            symbol=symbol,
            marginType=str(margin_type).upper(),
        )

    async def change_leverage(self, symbol, leverage):
        return await self._call(
            self.client.change_leverage,
            symbol=symbol,
            leverage=int(leverage),
        )

    async def get_commission_rate(self, symbol):
        return await self._call(self.client.commission_rate, symbol=symbol)

    async def get_exchange_info(self):
        return await self._call(self.client.exchange_info)

    async def get_positions(self, symbol=None):
        params = {"symbol": symbol} if symbol else {}
        result, status = await self._call(self.client.get_position_risk, **params)
        return (result if status == 200 else []), status

    async def get_open_orders(self, symbol=None):
        params = {"symbol": symbol} if symbol else {}
        return await self._call(self.client.get_orders, **params)

    async def get_all_orders(
        self, symbol, start_time=None, limit=1000, end_time=None, max_pages=200
    ):
        page_size = min(1000, max(1, int(limit)))
        fixed_end = int(end_time) if end_time is not None else (
            int(time.time() * 1000) if start_time is not None else None
        )
        base = {"symbol": symbol, "limit": page_size}
        if start_time is not None:
            base["startTime"] = int(start_time)
        if fixed_end is not None:
            base["endTime"] = fixed_end

        rows, seen, next_order_id = [], set(), None
        for _ in range(max(1, int(max_pages))):
            params = dict(base)
            if next_order_id is not None:
                params.pop("startTime", None)
                params["orderId"] = next_order_id

            batch, status = await self._call(self.client.get_all_orders, **params)
            if status != 200:
                return batch, status
            if not isinstance(batch, list):
                return self._page_error("allOrders returned a non-list payload")

            max_order_id = None
            for row in batch:
                if not isinstance(row, dict):
                    continue
                raw_id = row.get("orderId")
                try:
                    order_id = int(raw_id)
                except (TypeError, ValueError):
                    return self._page_error("allOrders page is missing a numeric orderId")
                max_order_id = order_id if max_order_id is None else max(max_order_id, order_id)
                if order_id not in seen:
                    seen.add(order_id)
                    rows.append(row)

            if len(batch) < page_size:
                return rows, 200
            if max_order_id is None:
                return self._page_error("allOrders full page has no usable cursor")
            candidate = max_order_id + 1
            if next_order_id is not None and candidate <= next_order_id:
                return self._page_error("allOrders cursor did not advance")
            next_order_id = candidate

        return self._page_error("allOrders exceeded pagination safety limit")

    async def get_income_history(
        self, symbol=None, start_time=None, limit=1000, end_time=None, max_pages=200
    ):
        page_size = min(1000, max(1, int(limit)))
        fixed_end = int(end_time) if end_time is not None else (
            int(time.time() * 1000) if start_time is not None else None
        )
        base = {"limit": page_size}
        if symbol:
            base["symbol"] = symbol
        if start_time is not None:
            base["startTime"] = int(start_time)
        if fixed_end is not None:
            base["endTime"] = fixed_end

        rows, seen = [], set()
        for page in range(1, max(1, int(max_pages)) + 1):
            params = dict(base)
            params["page"] = page
            batch, status = await self._call(self.client.get_income_history, **params)
            if status != 200:
                return batch, status
            if not isinstance(batch, list):
                return self._page_error("income history returned a non-list payload")

            for row in batch:
                if not isinstance(row, dict):
                    continue
                tran_id = row.get("tranId")
                key = (
                    ("tranId", str(tran_id))
                    if tran_id not in (None, "")
                    else (
                        "row",
                        str(row.get("time", "")),
                        str(row.get("incomeType", "")),
                        str(row.get("income", "")),
                        str(row.get("asset", "")),
                        str(row.get("symbol", "")),
                        str(row.get("info", "")),
                    )
                )
                if key not in seen:
                    seen.add(key)
                    rows.append(row)

            if len(batch) < page_size:
                return rows, 200

        return self._page_error("income history exceeded pagination safety limit")

    async def new_order(self, symbol, side, type, quantity=None, **kwargs):
        params = {"symbol": symbol, "side": side, "type": type}
        if quantity is not None:
            params["quantity"] = quantity
        params.update(kwargs)
        return await self._call(self.client.new_order, **params)

    async def cancel_all_open_orders(self, symbol):
        return await self._call(self.client.cancel_open_orders, symbol=symbol)

    async def cancel_order(self, symbol, order_id):
        return await self._call(self.client.cancel_order, symbol=symbol, orderId=order_id)

    async def query_order(self, symbol, client_order_id):
        return await self._call(
            self.client.query_order,
            symbol=symbol,
            origClientOrderId=client_order_id,
        )

    async def get_account_trades(self, symbol, start_time=None):
        params = {"symbol": symbol, "limit": 1000}
        if start_time is not None:
            params["startTime"] = int(start_time)
        return await self._call(self.client.get_account_trades, **params)

    async def new_algo_order(self, **params):
        payload = {"algoType": "CONDITIONAL", **params}
        return await self._call(
            self.client.sign_request,
            "POST",
            "/fapi/v1/algoOrder",
            payload,
        )

    async def query_algo_order(self, algo_id):
        return await self._call(
            self.client.sign_request,
            "GET",
            "/fapi/v1/algoOrder",
            {"algoId": algo_id},
        )

    async def get_open_algo_orders(self, symbol=None):
        params = {"symbol": symbol} if symbol else {}
        return await self._call(
            self.client.sign_request,
            "GET",
            "/fapi/v1/openAlgoOrders",
            params,
        )

    async def cancel_algo_order(self, algo_id):
        return await self._call(
            self.client.sign_request,
            "DELETE",
            "/fapi/v1/algoOrder",
            {"algoId": algo_id},
        )

    async def cancel_all_algo_orders(self, symbol):
        return await self._call(
            self.client.sign_request,
            "DELETE",
            "/fapi/v1/algoOpenOrders",
            {"symbol": symbol},
        )

    async def close(self):
        return None
