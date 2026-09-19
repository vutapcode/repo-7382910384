"""Mirror durable SHADOW ENTRY/EXIT events to Binance USD-M Futures testnet.

This process is deliberately separate from the trading engine. It has no
Market Truth, Guardian, or Hard Risk authority and never places exchange SLs.
Only a shadow fill (not a pending maker intent) can create demo exposure.
"""

from __future__ import annotations

import ast
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from binance.um_futures import UMFutures
from binance.error import ClientError
from loi_he_thong import journal_segments


TESTNET_URL = "https://testnet.binancefuture.com"
SYMBOL = "BTCUSDT"
SOURCE = Path(os.getenv(
    "WSTRADE_DEMO_SOURCE",
    "/home/ubuntu/.local/state/smc2026/mainnet_shadow/events.jsonl",
))
STATE_PATH = Path(os.getenv(
    "WSTRADE_DEMO_MIRROR_STATE",
    "/home/ubuntu/.local/state/wstrade/demo_mirror_state.json",
))
CREDENTIAL_SOURCE = Path(os.getenv(
    "WSTRADE_DEMO_CREDENTIAL_SOURCE", "/home/ubuntu/file cần tích hợp.py",
))
POLL_SECONDS = 1.0
MAKER_WAIT_SECONDS = 0.75


def _credentials(path):
    """Read literal keys from the supplied file; never execute/import it."""
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise RuntimeError("DEMO_CREDENTIAL_FILE_PERMISSIONS_TOO_OPEN")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values = {}
    for row in tree.body:
        if not isinstance(row, ast.Assign) or len(row.targets) != 1:
            continue
        target = row.targets[0]
        if isinstance(target, ast.Name) and target.id in {"API_KEY", "SECRET_KEY"}:
            values[target.id] = ast.literal_eval(row.value)
    if not all(isinstance(values.get(name), str) and values[name] for name in (
        "API_KEY", "SECRET_KEY",
    )):
        raise RuntimeError("DEMO_CREDENTIALS_MISSING")
    return values["API_KEY"], values["SECRET_KEY"]


def _atomic_state(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(row, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _order_id(event_id, suffix):
    digest = hashlib.sha256(str(event_id).encode("utf-8")).hexdigest()[:24]
    return "wsd%s%s" % (suffix, digest)


class DemoMirror:
    def __init__(self, client, *, source=SOURCE, state_path=STATE_PATH):
        self.client = client
        self.source = Path(source)
        self.state_path = Path(state_path)
        self.hedge = bool(client.get_position_mode()["dualSidePosition"])
        symbol = next(
            (row for row in client.exchange_info().get("symbols", ())
             if row.get("symbol") == SYMBOL), None,
        )
        if not symbol:
            raise RuntimeError("DEMO_SYMBOL_NOT_AVAILABLE")
        filters = {
            row["filterType"]: row
            for row in symbol.get("filters", ())
        }
        self.price_tick = Decimal(
            str(filters["PRICE_FILTER"]["tickSize"])
        )
        self.qty_step = Decimal(str(filters["LOT_SIZE"]["stepSize"]))
        self.min_qty = Decimal(str(filters["LOT_SIZE"]["minQty"]))
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state.get("version") != 1:
                raise RuntimeError("DEMO_MIRROR_STATE_VERSION_INVALID")
        else:
            # Never replay old shadow history into a new demo account.
            stat = self.source.stat()
            self.state = {
                "version": 1, "device": stat.st_dev, "inode": stat.st_ino,
                "offset": stat.st_size, "open": None, "pending": None,
            }
        self._assert_account_matches_state()
        _atomic_state(self.state_path, self.state)

    def _position_amount(self, side):
        rows = self.client.get_position_risk(symbol=SYMBOL)
        if self.hedge:
            row = next((r for r in rows if r.get("positionSide") == side), None)
            return abs(float((row or {}).get("positionAmt", 0) or 0))
        row = next((r for r in rows if r.get("positionSide") == "BOTH"), None)
        amount = float((row or {}).get("positionAmt", 0) or 0)
        return max(0.0, amount if side == "LONG" else -amount)

    def _assert_account_matches_state(self):
        owned = dict(self.state.get("open") or {})
        pending = dict(self.state.get("pending") or {})
        open_orders = self.client.get_orders(symbol=SYMBOL)
        allowed_ids = (
            {_order_id(pending["event_id"], suffix) for suffix in ("L", "M", "X")}
            if pending else set()
        )
        if any(order.get("clientOrderId") not in allowed_ids for order in open_orders):
            raise RuntimeError("DEMO_UNOWNED_OPEN_ORDER_REFUSE_MIRROR")
        long_qty = self._position_amount("LONG")
        short_qty = self._position_amount("SHORT")
        if pending:
            side = pending["side"]
            current = long_qty if side == "LONG" else short_qty
            opposite = short_qty if side == "LONG" else long_qty
            expected = float(pending["qty"])
            if opposite > 1e-9 or current > expected + 1e-9:
                raise RuntimeError("DEMO_PENDING_POSITION_DIVERGED")
            if pending["action"] == "ENTRY" and not owned:
                return
            if pending["action"] == "EXIT" and owned:
                return
            raise RuntimeError("DEMO_PENDING_STATE_INVALID")
        if not owned:
            if long_qty > 1e-9 or short_qty > 1e-9:
                raise RuntimeError("DEMO_ACCOUNT_NOT_FLAT_REFUSE_MIRROR")
        elif (
            abs(self._position_amount(owned["side"]) - float(owned["qty"])) > 1e-9
            or self._position_amount("SHORT" if owned["side"] == "LONG" else "LONG") > 1e-9
        ):
            raise RuntimeError("DEMO_POSITION_DIVERGED_REFUSE_MIRROR")

    def _submit(self, event_id, suffix, **kwargs):
        client_id = _order_id(event_id, suffix)
        try:
            return self.client.query_order(
                symbol=SYMBOL, origClientOrderId=client_id,
            )
        except ClientError as exc:
            if int(exc.error_code) != -2013:
                raise
        except LookupError:
            # Test fake's equivalent of Binance ORDER_DOES_NOT_EXIST.
            pass
        try:
            return self.client.new_order(
                symbol=SYMBOL, newClientOrderId=client_id, **kwargs,
            )
        except Exception:
            # A transport timeout may mean Binance accepted the order. Query
            # the deterministic client ID before any retry can submit again.
            return self.client.query_order(
                symbol=SYMBOL, origClientOrderId=client_id,
            )

    def _begin(self, action, row, side, qty):
        pending = dict(self.state.get("pending") or {})
        expected = {
            "action": action,
            "event_id": str(row["event_id"]),
            "cycle_id": str(row["cycle_id"]),
            "side": side,
            "qty": float(qty),
        }
        if pending:
            if pending != expected:
                raise RuntimeError("DEMO_PENDING_EVENT_MISMATCH")
        else:
            self._assert_account_matches_state()
            self.state["pending"] = expected
            _atomic_state(self.state_path, self.state)

    def _open(self, row):
        if self.state.get("open"):
            raise RuntimeError("DEMO_PREVIOUS_CYCLE_STILL_OPEN")
        self._assert_account_matches_state()
        cycle_id = str(row.get("cycle_id") or "")
        side = str(row.get("side") or "").upper()
        qty = float(row.get("actual_qty_btc") or row.get("qty_btc") or 0)
        if not cycle_id or side not in {"LONG", "SHORT"} or qty <= 0:
            raise RuntimeError("SHADOW_ENTRY_IDENTITY_INVALID")
        qty_decimal = Decimal(str(qty))
        if (
            qty_decimal < self.min_qty
            or qty_decimal % self.qty_step != 0
        ):
            raise RuntimeError("SHADOW_QTY_NOT_DEMO_EXECUTABLE")
        self._begin("ENTRY", row, side, qty)
        if abs(self._position_amount(side) - qty) <= 1e-9:
            self.state["open"] = {"cycle_id": cycle_id, "side": side, "qty": qty}
            self.state["pending"] = None
            return
        order_side = "BUY" if side == "LONG" else "SELL"
        common = {"side": order_side, "quantity": qty}
        if self.hedge:
            common["positionSide"] = side
        style = str((row.get("shadow_execution") or {}).get("style") or "MARKET")
        if style == "MAKER_TRADE_THROUGH":
            raw_price = Decimal(str(row.get("price") or 0))
            if raw_price <= 0:
                raise RuntimeError("SHADOW_MAKER_PRICE_MISSING")
            rounding = ROUND_FLOOR if side == "LONG" else ROUND_CEILING
            price = float(
                (raw_price / self.price_tick).to_integral_value(rounding=rounding)
                * self.price_tick
            )
            placed = self._submit(
                row["event_id"], "L", type="LIMIT", price=price,
                timeInForce="GTC", **common,
            )
            order_id = placed.get("orderId")
            deadline = time.monotonic() + MAKER_WAIT_SECONDS
            status = placed
            while time.monotonic() < deadline:
                status = self.client.query_order(symbol=SYMBOL, orderId=order_id)
                if status.get("status") in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                    break
                time.sleep(0.1)
            if status.get("status") not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                self.client.cancel_order(symbol=SYMBOL, orderId=order_id)
                status = self.client.query_order(symbol=SYMBOL, orderId=order_id)
            if status.get("status") not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                raise RuntimeError("DEMO_LIMIT_FINAL_STATUS_UNKNOWN")
            remaining = max(0.0, qty - float(status.get("executedQty", 0) or 0))
        else:
            remaining = qty
        if remaining > 1e-9:
            self._submit(row["event_id"], "M", type="MARKET", quantity=remaining,
                         **{k: v for k, v in common.items() if k != "quantity"})
        actual = self._position_amount(side)
        if abs(actual - qty) > 1e-9:
            raise RuntimeError("DEMO_ENTRY_QTY_DIVERGED")
        self.state["open"] = {"cycle_id": cycle_id, "side": side, "qty": actual}
        self.state["pending"] = None

    def _close(self, row):
        owned = dict(self.state.get("open") or {})
        if not owned:
            raise RuntimeError("SHADOW_EXIT_WITHOUT_MIRRORED_ENTRY")
        if str(row.get("cycle_id") or "") != owned["cycle_id"]:
            raise RuntimeError("SHADOW_EXIT_CYCLE_MISMATCH")
        side = owned["side"]
        self._begin("EXIT", row, side, owned["qty"])
        if self._position_amount(side) <= 1e-9:
            self.state["open"] = None
            self.state["pending"] = None
            return
        params = {
            "side": "SELL" if side == "LONG" else "BUY",
            "type": "MARKET", "quantity": owned["qty"],
        }
        if self.hedge:
            params["positionSide"] = side
        else:
            params["reduceOnly"] = "true"
        # No exchange SL, STOP_MARKET, or closePosition flag is ever sent.
        self._submit(row["event_id"], "X", **params)
        if self._position_amount(side) > 1e-9:
            raise RuntimeError("DEMO_EXIT_NOT_FLAT")
        self.state["open"] = None
        self.state["pending"] = None

    def process(self, row):
        if not row.get("event_id"):
            return
        event = str(row.get("event") or "").upper()
        if event == "ENTRY":
            self._open(row)
            print(
                "[DEMO-MIRROR] ENTRY cycle=%s side=%s qty=%s"
                % (row.get("cycle_id"), row.get("side"),
                   self.state["open"]["qty"]),
                flush=True,
            )
        elif event == "EXIT":
            self._close(row)
            print(
                "[DEMO-MIRROR] EXIT cycle=%s reason=%s"
                % (row.get("cycle_id"), row.get("risk_reason")),
                flush=True,
            )

    def run_once(self):
        if (self.state["device"], self.state["inode"]) not in {
            (p.stat().st_dev, p.stat().st_ino)
            for p in journal_segments.ordered_paths(self.source)
        }:
            raise RuntimeError("DEMO_JOURNAL_CURSOR_LOST")
        sources = journal_segments.cursor_sources(
            self.source, self.state["device"], self.state["inode"],
            self.state["offset"],
        )
        processed = 0
        for path, start in sources:
            stat = path.stat()
            with path.open("rb") as handle:
                handle.seek(start)
                while True:
                    offset = handle.tell()
                    line = handle.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    row = json.loads(line)
                    critical = str(row.get("event") or "").upper() in {
                        "ENTRY", "EXIT",
                    }
                    self.process(row)
                    self.state.update(
                        device=stat.st_dev, inode=stat.st_ino,
                        offset=handle.tell(),
                    )
                    if critical:
                        _atomic_state(self.state_path, self.state)
                    processed += 1
        if processed:
            _atomic_state(self.state_path, self.state)
        return processed


def main():
    key, secret = _credentials(CREDENTIAL_SOURCE)
    client = UMFutures(
        key=key, secret=secret, base_url=TESTNET_URL, timeout=5,
    )
    if client.base_url != TESTNET_URL:
        raise RuntimeError("DEMO_URL_NOT_TESTNET")
    mirror = DemoMirror(client)
    print("[DEMO-MIRROR] ready testnet-only cursor=%s" % mirror.state["offset"], flush=True)
    while True:
        mirror.run_once()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
