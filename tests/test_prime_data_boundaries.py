from collections import deque
import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace

from loi_he_thong import liquidation_context
from loi_he_thong import tier_s_runtime_prune


ROOT = Path(__file__).resolve().parents[1]


def _load(name, relative_path):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_force_order_runtime_has_one_ingest_path_and_dedupes():
    source = inspect.getsource(tier_s_runtime_prune._lean_main)
    assert "hung_force_order_futures" not in source
    assert "force_order_observer=liquidation_context.observe_force_order" in source
    assert "force_order_epoch_reset=liquidation_context.reset_epoch" in source

    state = SimpleNamespace(
        long_liquidation_quote_total=0.0,
        short_liquidation_quote_total=0.0,
        liquidation_events=deque(maxlen=128),
    )
    liquidation_context.reset_epoch(state)
    payload = {
        "e": "forceOrder",
        "E": 1_000,
        "o": {
            "i": 123,
            "S": "SELL",
            "p": "100.0",
            "q": "2.0",
            "T": 1_000,
        },
    }
    assert liquidation_context.observe_force_order(state, payload, 1_001) == "LIQUIDATION"
    assert liquidation_context.observe_force_order(state, payload, 1_001) == "DUPLICATE"
    assert state.long_liquidation_quote_total == 200.0
    assert len(state.liquidation_events) == 1


def test_coinbase_l2_is_namespaced_and_authority_false():
    cb = _load(
        "prime_test_tai_coinbase",
        "1_tai_du_lieu/tai_coinbase/tai_coinbase.py",
    )
    assert cb.COINBASE_WS_MAX_SIZE >= 2 * 1024 * 1024
    book = cb.CoinbaseL2Book()
    assert book.snapshot({
        "bids": [["100", "1.0"], ["99", "2.0"]],
        "asks": [["101", "1.5"], ["102", "3.0"]],
    })
    assert book.apply({
        "changes": [["sell", "101", "1.0"], ["buy", "100", "1.25"]],
    })

    state = SimpleNamespace(coinbase_l2_epoch=0)
    cb._reset_l2_state(state, book)
    # reset starts a new epoch; use a fresh book snapshot before publication.
    assert book.snapshot({
        "bids": [["100", "1.25"]],
        "asks": [["101", "1.0"]],
    })
    assert cb._publish_l2(
        state,
        book,
        {"time": "2026-09-03T11:00:00Z"},
        1_788_430_800_000,
        force=True,
    )
    snap = state.coinbase_l2_snapshot
    assert snap["authority"] is False
    assert snap["semantic_role"] == "USD_CASH_LIQUIDITY_DATA_ONLY"
    assert snap["source_id"] == "coinbase_btcusd_l2"
    assert not hasattr(state, "bias_state")
    assert not hasattr(state, "best_bid")


def test_liquidity_response_refuses_raw_l2_as_execution():
    response = _load(
        "prime_test_cash_liquidity_response",
        "2_suy_luan_mapping/cash_liquidity_response.py",
    )
    raw_only = response.classify({
        "side": "LONG",
        "executed_quote": 100_000,
        "execution_linked": False,
        "price_progress_bps": 2.0,
        "opposing_depletion_quote": 50_000,
    })
    assert raw_only["state"] == "UNKNOWN"
    assert raw_only["reason"] == "L2_NOT_LINKED_TO_EXECUTION"
    assert raw_only["authority"] is False

    linked = response.classify({
        "side": "LONG",
        "executed_quote": 100_000,
        "execution_linked": True,
        "price_progress_bps": 2.0,
        "opposing_depletion_quote": 50_000,
        "opposing_refill_quote": 10_000,
    })
    assert linked["state"] == "FLOW_CONVERTING"
    assert linked["authority"] is False
    assert linked["can_create_direction"] is False


def test_usdt_basis_is_direct_non_btc_normalization():
    basis = _load(
        "prime_test_usdt_usd",
        "1_tai_du_lieu/tai_usdt_usd/tai_usdt_usd.py",
    )
    snap = basis.parse_ticker({
        "type": "ticker",
        "price": "0.9995",
        "best_bid": "0.9994",
        "best_ask": "0.9996",
        "time": "2026-09-03T11:00:00Z",
        "sequence": 7,
    }, receive_time_ms=1_788_430_800_100, epoch=3)
    assert snap["source_id"] == "coinbase_usdt_usd"
    assert snap["instrument"] == "USDT-USD"
    assert snap["authority"] is False
    assert snap["semantic_role"] == "USDT_USD_BASIS_DATA_ONLY"
    assert round(snap["basis_bps_vs_par"], 6) == -5.0
    assert "btc" not in " ".join(snap.keys()).lower()
