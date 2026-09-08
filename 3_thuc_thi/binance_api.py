"""Async Binance USD-M Futures MAINNET REST/Algo adapter."""

import asyncio
import logging
import time

from binance.error import ClientError
from binance.um_futures import UMFutures
from loi_he_thong import execution_control_plane


class BinanceAPI:
    BASE_URL = "https://fapi.binance.com"
    supports_execution_deadline = True
    DEADLINE_CONTRACT_VERSION = "BINANCE_TRANSPORT_DEADLINE_V1"

    def __init__(self, api_key, secret_key):
        self.base_url = self.BASE_URL
        self.client = UMFutures(
            key=api_key,
            secret=secret_key,
            base_url=self.base_url,
            timeout=10,
        )
        # Measurements are execution facts, not market alpha.  The production
        # adapter is allowed to reject a *new entry* when its observed p95 no
        # longer fits the sealed opportunity budget.  Exit/recovery callers do
        # not pass through this admission gate.
        self._control_plane = execution_control_plane.Monitor(
            latency_authority_enabled=True,
        )

    @staticmethod
    def _error_payload(exc):
        if isinstance(exc, ClientError):
            return {"code": exc.error_code, "message": exc.error_message}, exc.status_code
        return {"code": "NETWORK", "message": str(exc)}, 599

    @staticmethod
    def _deadline_error(operation, timeout_seconds):
        return {
            "code": "EXECUTION_DEADLINE_EXCEEDED",
            "message": "transport did not complete inside the sealed deadline",
            "operation": str(operation or "UNKNOWN"),
            "timeout_seconds": round(max(0.0, float(timeout_seconds)), 6),
            "deadline_contract_version": BinanceAPI.DEADLINE_CONTRACT_VERSION,
        }, 599

    def _deadline_client(self, timeout_seconds):
        """Create a request-local client so concurrent calls cannot race timeout."""
        source = self.client
        return UMFutures(
            key=getattr(source, "key", None),
            secret=getattr(source, "secret", None),
            base_url=getattr(source, "base_url", self.base_url),
            timeout=max(0.001, float(timeout_seconds)),
            proxies=getattr(source, "proxies", None),
            show_limit_usage=bool(getattr(source, "show_limit_usage", False)),
            show_header=bool(getattr(source, "show_header", False)),
            private_key=getattr(source, "private_key", None),
            private_key_passphrase=getattr(
                source, "private_key_pass", None
            ),
        )

    async def _call(
        self, func, *args, operation=None, control=False,
        deadline_monotonic=None, timeout_cap_seconds=None, **kwargs
    ):
        operation = operation or getattr(func, "__name__", "UNKNOWN")
        timeout_seconds = None
        if deadline_monotonic is not None:
            timeout_seconds = max(
                0.0, float(deadline_monotonic) - time.monotonic()
            )
        if timeout_cap_seconds is not None:
            cap = max(0.0, float(timeout_cap_seconds))
            timeout_seconds = (
                cap if timeout_seconds is None else min(timeout_seconds, cap)
            )
        monitor = getattr(self, "_control_plane", None)
        token = monitor.begin(
            operation,
            control=bool(control),
        ) if monitor is not None else None

        if timeout_seconds is not None and timeout_seconds <= 0.0:
            if monitor is not None:
                monitor.complete(token, 599)
            return self._deadline_error(operation, timeout_seconds)

        def invoke():
            request_client = None
            try:
                target = func
                # The connector stores timeout on its client, not per request.
                # Never mutate that shared value: a concurrent safety call may
                # have a different deadline.  A request-local client gives the
                # underlying socket the same bound as the asyncio caller.
                if (
                    timeout_seconds is not None
                    and getattr(func, "__self__", None) is self.client
                ):
                    request_client = self._deadline_client(timeout_seconds)
                    target = getattr(request_client, func.__name__)
                return target(*args, **kwargs), 200
            except Exception as exc:
                return self._error_payload(exc)
            finally:
                session = getattr(request_client, "session", None)
                if session is not None:
                    session.close()
        try:
            work = asyncio.to_thread(invoke)
            if timeout_seconds is None:
                result, status = await work
            else:
                result, status = await asyncio.wait_for(
                    work, timeout=max(0.001, timeout_seconds)
                )
        except asyncio.TimeoutError:
            if monitor is not None:
                monitor.complete(token, 599)
            return self._deadline_error(operation, timeout_seconds)
        except BaseException:
            if monitor is not None:
                monitor.complete(token, 599)
            raise
        if monitor is not None:
            monitor.complete(token, status)
        return result, status

    def control_plane_snapshot(
        self, opportunity_budget_ms=None, has_exposure=False
    ):
        monitor = getattr(self, "_control_plane", None)
        if monitor is None:
            return {
                "version": execution_control_plane.VERSION,
                "health": "UNKNOWN",
                "reason": "MONITOR_NOT_INITIALIZED",
                "entry_allowed": False,
                "sample_count": 0,
            }
        return monitor.snapshot(
            opportunity_budget_ms=opportunity_budget_ms,
            has_exposure=has_exposure,
        )

    @staticmethod
    def _page_error(message):
        return {"code": "PAGINATION", "message": message}, 599

    async def get_balance(self):
        result, status = await self._call(
            self.client.balance, operation="BALANCE", control=True
        )
        if status != 200:
            logging.error("[API] balance failed: %s", result)
            return 0.0
        for item in result:
            if item.get("asset") == "USDT":
                return float(item.get("balance", item.get("availableBalance", 0.0)))
        return 0.0

    async def get_balance_details(self):
        result, status = await self._call(
            self.client.balance, operation="BALANCE_DETAILS", control=True
        )
        if status != 200:
            return {}, status
        row = next((item for item in result if item.get("asset") == "USDT"), {})
        return row, status

    async def get_position_mode(self):
        result, status = await self._call(
            self.client.get_position_mode,
            operation="GET_POSITION_MODE", control=True,
        )
        if status != 200:
            return None
        return bool(result.get("dualSidePosition"))

    async def change_position_mode(self, dual_side=True):
        return await self._call(
            self.client.change_position_mode,
            operation="CHANGE_POSITION_MODE", control=True,
            dualSidePosition="true" if dual_side else "false",
        )

    async def get_multi_asset_mode(self):
        return await self._call(
            self.client.get_multi_asset_mode,
            operation="GET_MULTI_ASSET_MODE", control=True,
        )

    async def change_multi_asset_mode(self, enabled=False):
        return await self._call(
            self.client.change_multi_asset_mode,
            operation="CHANGE_MULTI_ASSET_MODE", control=True,
            multiAssetsMargin="true" if enabled else "false",
        )

    async def change_margin_type(self, symbol, margin_type="ISOLATED"):
        return await self._call(
            self.client.change_margin_type,
            operation="CHANGE_MARGIN_TYPE", control=True,
            symbol=symbol,
            marginType=str(margin_type).upper(),
        )

    async def change_leverage(self, symbol, leverage):
        return await self._call(
            self.client.change_leverage,
            operation="CHANGE_LEVERAGE", control=True,
            symbol=symbol,
            leverage=int(leverage),
        )

    async def get_commission_rate(self, symbol):
        return await self._call(
            self.client.commission_rate, symbol=symbol,
            operation="COMMISSION_RATE", control=True,
        )

    async def get_exchange_info(self):
        return await self._call(self.client.exchange_info)

    async def get_positions(
        self, symbol=None, *, deadline_monotonic=None,
        timeout_cap_seconds=None,
    ):
        params = {"symbol": symbol} if symbol else {}
        result, status = await self._call(
            self.client.get_position_risk, **params,
            operation="GET_POSITIONS", control=True,
            deadline_monotonic=deadline_monotonic,
            timeout_cap_seconds=timeout_cap_seconds,
        )
        return (result if status == 200 else []), status

    async def get_open_orders(
        self, symbol=None, *, deadline_monotonic=None,
        timeout_cap_seconds=None,
    ):
        params = {"symbol": symbol} if symbol else {}
        return await self._call(
            self.client.get_orders, **params,
            operation="GET_OPEN_ORDERS", control=True,
            deadline_monotonic=deadline_monotonic,
            timeout_cap_seconds=timeout_cap_seconds,
        )

    async def new_listen_key(self):
        return await self._call(
            self.client.new_listen_key,
            operation="NEW_LISTEN_KEY", control=True,
        )

    async def renew_listen_key(self, listen_key):
        return await self._call(
            self.client.renew_listen_key, listenKey=listen_key,
            operation="RENEW_LISTEN_KEY", control=True,
        )

    async def close_listen_key(self, listen_key):
        return await self._call(
            self.client.close_listen_key, listenKey=listen_key,
            operation="CLOSE_LISTEN_KEY", control=True,
        )

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

            batch, status = await self._call(
                self.client.get_all_orders, **params,
                operation="GET_ALL_ORDERS", control=True,
            )
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
            batch, status = await self._call(
                self.client.get_income_history, **params,
                operation="GET_INCOME_HISTORY", control=True,
            )
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

    async def new_order(
        self, symbol, side, type, quantity=None, *,
        deadline_monotonic=None, timeout_cap_seconds=None, **kwargs
    ):
        params = {"symbol": symbol, "side": side, "type": type}
        if quantity is not None:
            params["quantity"] = quantity
        params.update(kwargs)
        return await self._call(
            self.client.new_order, **params,
            operation="NEW_ORDER", control=True,
            deadline_monotonic=deadline_monotonic,
            timeout_cap_seconds=timeout_cap_seconds,
        )

    async def cancel_all_open_orders(
        self, symbol, *, deadline_monotonic=None,
        timeout_cap_seconds=None,
    ):
        return await self._call(
            self.client.cancel_open_orders, symbol=symbol,
            operation="CANCEL_ALL_OPEN_ORDERS", control=True,
            deadline_monotonic=deadline_monotonic,
            timeout_cap_seconds=timeout_cap_seconds,
        )

    async def cancel_order(
        self, symbol, order_id=None, *, client_order_id=None,
        deadline_monotonic=None, timeout_cap_seconds=None,
    ):
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        elif client_order_id:
            params["origClientOrderId"] = str(client_order_id)
        else:
            return {"code": "ORDER_IDENTITY_REQUIRED"}, 400
        return await self._call(
            self.client.cancel_order, **params,
            operation="CANCEL_ORDER", control=True,
            deadline_monotonic=deadline_monotonic,
            timeout_cap_seconds=timeout_cap_seconds,
        )

    async def query_order(
        self, symbol, client_order_id, *, deadline_monotonic=None,
        timeout_cap_seconds=None,
    ):
        return await self._call(
            self.client.query_order,
            operation="QUERY_ORDER", control=True,
            symbol=symbol,
            origClientOrderId=client_order_id,
            deadline_monotonic=deadline_monotonic,
            timeout_cap_seconds=timeout_cap_seconds,
        )

    async def get_account_trades(self, symbol, start_time=None):
        params = {"symbol": symbol, "limit": 1000}
        if start_time is not None:
            params["startTime"] = int(start_time)
        return await self._call(
            self.client.get_account_trades, **params,
            operation="GET_ACCOUNT_TRADES", control=True,
        )

    async def new_algo_order(
        self, *, deadline_monotonic=None, timeout_cap_seconds=None, **params
    ):
        payload = {"algoType": "CONDITIONAL", **params}
        return await self._call(
            self.client.sign_request,
            "POST",
            "/fapi/v1/algoOrder",
            payload,
            operation="NEW_ALGO_ORDER", control=True,
            deadline_monotonic=deadline_monotonic,
            timeout_cap_seconds=timeout_cap_seconds,
        )

    async def query_algo_order(self, algo_id):
        return await self._call(
            self.client.sign_request,
            "GET",
            "/fapi/v1/algoOrder",
            {"algoId": algo_id},
            operation="QUERY_ALGO_ORDER", control=True,
        )

    async def get_open_algo_orders(
        self, symbol=None, *, deadline_monotonic=None,
        timeout_cap_seconds=None,
    ):
        params = {"symbol": symbol} if symbol else {}
        return await self._call(
            self.client.sign_request,
            "GET",
            "/fapi/v1/openAlgoOrders",
            params,
            operation="GET_OPEN_ALGO_ORDERS", control=True,
            deadline_monotonic=deadline_monotonic,
            timeout_cap_seconds=timeout_cap_seconds,
        )

    async def cancel_algo_order(
        self, algo_id, *, deadline_monotonic=None,
        timeout_cap_seconds=None,
    ):
        return await self._call(
            self.client.sign_request,
            "DELETE",
            "/fapi/v1/algoOrder",
            {"algoId": algo_id},
            operation="CANCEL_ALGO_ORDER", control=True,
            deadline_monotonic=deadline_monotonic,
            timeout_cap_seconds=timeout_cap_seconds,
        )

    async def cancel_all_algo_orders(
        self, symbol, *, deadline_monotonic=None,
        timeout_cap_seconds=None,
    ):
        return await self._call(
            self.client.sign_request,
            "DELETE",
            "/fapi/v1/algoOpenOrders",
            {"symbol": symbol},
            operation="CANCEL_ALL_ALGO_ORDERS", control=True,
            deadline_monotonic=deadline_monotonic,
            timeout_cap_seconds=timeout_cap_seconds,
        )

    async def close(self):
        return None
