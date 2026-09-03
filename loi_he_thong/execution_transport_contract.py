"""Phase-7 secondary execution transport research contract.

Preparation only: no network calls, no credentials, no runtime authority.
"""
from __future__ import annotations

import hashlib
import re

VERSION = "EXECUTION_TRANSPORT_CONTRACT_V1"
AUTHORITY = False
SUBMIT_RESULTS = frozenset({"ACK", "REJECT", "UNKNOWN"})
PRIMARY_TRANSPORT = "BINANCE_USDM_REST"
SECONDARY_TRANSPORT = "BINANCE_USDM_AUTHENTICATED_WEBSOCKET_API"
FAILURE_DOMAIN_RELATION = "CORRELATED_BINANCE_CONTROL_PLANE_NOT_INDEPENDENT"
CLIENT_ID_RE = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")


def canonical_client_order_id(run_id, intent_id, purpose, side):
    raw = f"{run_id}|{intent_id}|{purpose}|{str(side).upper()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    value = f"ws7_{str(purpose).lower()[:5]}_{digest}"[:36]
    if not CLIENT_ID_RE.match(value):
        raise ValueError("CLIENT_ORDER_ID_INVALID")
    return value


def normalize_submit_result(status_code=None, payload=None, transport_error=False):
    if transport_error:
        return "UNKNOWN"
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if 200 <= code < 300:
        return "ACK"
    if payload is not None:
        return "REJECT"
    return "UNKNOWN"


def after_submit(result, *, client_order_id, reconciliation=None, fallback_transport=None,
                 in_flight_transport=None):
    result = str(result).upper()
    if result not in SUBMIT_RESULTS:
        raise ValueError("SUBMIT_RESULT_INVALID")
    if result == "ACK":
        return {"next":"DONE","resubmit_allowed":False,"client_order_id":client_order_id}
    if result == "REJECT":
        return {"next":"STOP","resubmit_allowed":False,"client_order_id":client_order_id}
    if reconciliation is None:
        return {"next":"RECONCILE_REQUIRED","resubmit_allowed":False,"client_order_id":client_order_id}
    state = str(reconciliation).upper()
    if state in {"FOUND_NEW", "FOUND_PARTIAL", "FOUND_FILLED", "FOUND_CANCELED", "FOUND_REJECTED"}:
        return {"next":"DONE_FROM_EXCHANGE_STATE","resubmit_allowed":False,"client_order_id":client_order_id}
    if state != "VERIFIED_NOT_FOUND":
        return {"next":"RECONCILE_REQUIRED","resubmit_allowed":False,"client_order_id":client_order_id}
    if in_flight_transport:
        return {"next":"WAIT_IN_FLIGHT","resubmit_allowed":False,"client_order_id":client_order_id}
    if fallback_transport not in {PRIMARY_TRANSPORT, SECONDARY_TRANSPORT}:
        return {"next":"NO_APPROVED_FALLBACK","resubmit_allowed":False,"client_order_id":client_order_id}
    return {
        "next":"FALLBACK_ELIGIBLE_AFTER_RECONCILIATION",
        "resubmit_allowed":True,
        "client_order_id":client_order_id,
        "required_client_order_id":client_order_id,
        "fallback_transport":fallback_transport,
    }


def transport_research_status():
    return {
        "authority": False,
        "primary": PRIMARY_TRANSPORT,
        "secondary_candidate": SECONDARY_TRANSPORT,
        "user_data_stream_is_submit_transport": False,
        "independent_failure_domains": False,
        "failure_domain_relation": FAILURE_DOMAIN_RELATION,
        "promotion_status": "AUTHENTICATED_TESTING_NOT_APPROVED",
    }
