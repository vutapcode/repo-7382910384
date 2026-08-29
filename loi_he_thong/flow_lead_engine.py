"""Trade-relative Cash/Perp context; never objective Spot price discovery."""
from collections import deque
import time

from loi_he_thong import ignition_signals

VERSION = "FLOW_DISPLACEMENT_ENGINE_V5_SCOPE_EXPLICIT"
MAX_SKEW_S = 0.30
ACTIVE_GAP_MS = 300

def _f(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0

def _flow_mean(row):
    vals = [
        _f(v.get("signed_imbalance"))
        for v in (row.get("venues") or {}).values()
        if isinstance(v, dict)
    ]
    return sum(vals) / len(vals) if vals else 0.0

def _ref(rows, now, age):
    target = now - age
    out = None
    for row in rows:
        if _f(row.get("ts")) <= target:
            out = row
        else:
            break
    return out

def _signed_bps(cur, ref, side):
    cur, ref = _f(cur), _f(ref)
    if cur <= 0.0 or ref <= 0.0:
        return None
    normalized = str(side).upper()
    if normalized not in ("LONG", "SHORT"):
        return None
    direction = 1.0 if normalized == "LONG" else -1.0
    return (cur - ref) / ref * 10000.0 * direction

def _freshness(state):
    spot = _f(getattr(state, "thoi_gian_tick_cuoi", 0.0))
    coinbase = _f(getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0))
    futures = _f(getattr(state, "execution_price_time", 0.0))
    values = [v for v in (spot, coinbase, futures) if v > 0.0]
    if len(values) < 3:
        return {"aligned": False, "skew_s": None}
    skew = max(values) - min(values)
    return {"aligned": skew <= MAX_SKEW_S, "skew_s": round(skew, 4)}


def _active_histories(state):
    """Incrementally adapt active Ignition buckets; never call retired Entry."""
    cache = getattr(state, "_flow_lead_active_cache", None)
    if not isinstance(cache, dict):
        cache = {
            "seen": {}, "epochs": {}, "pending": {}, "latest": {},
            "flow": deque(maxlen=64), "price": deque(maxlen=96),
            "last_flow_ms": 0, "last_price_ms": 0,
        }
        state._flow_lead_active_cache = cache

    engine = ignition_signals.engine(state)
    now_ms = int(time.time() * 1000)
    histories = engine.snapshot(now_ms)
    grouped = {}
    epoch_reset = any(
        venue in cache["epochs"]
        and int(cache["epochs"][venue]) != int(node.epoch)
        for venue, node in engine.venues.items()
    )
    for venue, rows in histories.items():
        seen = int(cache["seen"].get(venue, -1))
        for row in rows:
            token = int(row.get("bucket_start_ms", -1))
            if token <= seen:
                continue
            epoch = int(row.get("epoch", 0) or 0)
            previous_epoch = cache["epochs"].get(venue)
            if previous_epoch is not None and int(previous_epoch) != epoch:
                epoch_reset = True
            cache["epochs"][venue] = epoch
            grouped.setdefault(token, []).append((venue, row))
            seen = max(seen, token)
        cache["seen"][venue] = seen

    if epoch_reset:
        cache["pending"].clear()
        cache["latest"].clear()
        cache["flow"].clear()
        cache["price"].clear()
        cache["last_flow_ms"] = cache["last_price_ms"] = 0

    aliases = {
        "binance_spot": "spot", "coinbase_spot": "coinbase",
        "futures": "futures",
    }
    for token in sorted(grouped):
        pending = cache["pending"].setdefault(token, {"ts": token / 1000.0, "venues": {}})
        for venue, row in grouped[token]:
            alias = aliases[venue]
            pending["venues"][alias] = {
                "signed_imbalance": _f(row.get("imbalance")),
                "volume_btc": _f(row.get("total_qty")),
            }
            cache["latest"][alias] = {
                "price": _f(row.get("price")), "receive_ms": token,
                "epoch": int(row.get("epoch", 0) or 0),
            }
        latest = cache["latest"]
        if all(name in latest for name in ("spot", "coinbase", "futures")):
            stamps = [latest[name]["receive_ms"] for name in ("spot", "coinbase", "futures")]
            if max(stamps) - min(stamps) <= int(MAX_SKEW_S * 1000):
                if cache["last_price_ms"] and token - cache["last_price_ms"] > ACTIVE_GAP_MS:
                    cache["price"].clear()
                cache["price"].append({
                    "ts": token / 1000.0,
                    "spot": latest["spot"]["price"],
                    "coinbase": latest["coinbase"]["price"],
                    "futures": latest["futures"]["price"],
                })
                cache["last_price_ms"] = token

    cutoff = now_ms - 200
    for token in sorted(tuple(cache["pending"])):
        if token > cutoff:
            continue
        row = cache["pending"].pop(token)
        if len(row["venues"]) < 2:
            continue
        if cache["last_flow_ms"] and token - cache["last_flow_ms"] > ACTIVE_GAP_MS:
            cache["flow"].clear()
        cache["flow"].append(row)
        cache["last_flow_ms"] = token
    return tuple(cache["flow"]), tuple(cache["price"])

def analyze(state, side):
    """Return context only; never creates a trade signal."""
    normalized_side = str(side).upper()
    flow_rows, price_rows = _active_histories(state)
    flow_rows, price_rows = list(flow_rows)[-24:], list(price_rows)[-32:]
    fresh = _freshness(state)

    if not flow_rows or not price_rows:
        return {
            "version": VERSION, "status": "WARMUP", "persistence": 0.0,
            "oppose_ratio": 0.0, "displacement_dominance": "UNKNOWN",
            "lead_gap_bps": 0.0,
            "lead_accel_bps": 0.0, "freshness": fresh,
            "displacement_scope": "TRADE_RELATIVE_CASH_VS_PERP",
            "event_ordering_leader": "NOT_MEASURED",
            "event_ordering_authority": False,
            "spot_price_discovery": "NOT_MEASURED_HERE",
            "history_source": "ACTIVE_IGNITION_100MS",
        }

    if normalized_side not in ("LONG", "SHORT"):
        return {
            "version": VERSION, "status": "DIRECTION_UNAVAILABLE",
            "persistence": 0.0, "oppose_ratio": 0.0,
            "displacement_dominance": "UNKNOWN", "lead_gap_bps": 0.0,
            "lead_accel_bps": 0.0, "freshness": fresh,
            "displacement_scope": "TRADE_RELATIVE_CASH_VS_PERP",
            "event_ordering_leader": "NOT_MEASURED",
            "event_ordering_authority": False,
            "spot_price_discovery": "NOT_MEASURED_HERE",
            "history_source": "ACTIVE_IGNITION_100MS",
            "policy": "NO_SIDE_NO_TRADE_RELATIVE_INFERENCE",
        }

    recent = flow_rows[-12:]
    raw_means = [_flow_mean(r) for r in recent]
    direction = 1.0 if normalized_side == "LONG" else -1.0
    means = [direction * value for value in raw_means]
    persistence = sum(v >= 0.08 for v in means) / len(means) if means else 0.0
    oppose_ratio = sum(v <= -0.08 for v in means) / len(means) if means else 0.0

    if not fresh["aligned"]:
        return {
            "version": VERSION, "status": "UNALIGNED",
            "persistence": round(persistence, 4),
            "oppose_ratio": round(oppose_ratio, 4),
            "flow_mean": round(sum(means) / len(means), 4) if means else 0.0,
            "flow_mean_raw": round(sum(raw_means) / len(raw_means), 4) if raw_means else 0.0,
            "displacement_dominance": "UNKNOWN", "lead_gap_bps": 0.0,
            "lead_accel_bps": 0.0,
            "freshness": fresh,
            "displacement_scope": "TRADE_RELATIVE_CASH_VS_PERP",
            "event_ordering_leader": "NOT_MEASURED",
            "event_ordering_authority": False,
            "spot_price_discovery": "NOT_MEASURED_HERE",
            "history_source": "ACTIVE_IGNITION_100MS",
            "policy": "NO_LEAD_INFERENCE_WHEN_FEEDS_ARE_TIME_SKEWED",
        }

    now_row = price_rows[-1]
    now = _f(now_row.get("ts"))
    fast_ref = _ref(price_rows, now, 0.35)
    slow_ref = _ref(price_rows, now, 0.80)

    def gap(ref):
        if not ref:
            return 0.0
        spot = _signed_bps(now_row.get("spot"), ref.get("spot"), side)
        cb = _signed_bps(now_row.get("coinbase"), ref.get("coinbase"), side)
        fut = _signed_bps(now_row.get("futures"), ref.get("futures"), side)
        cash_vals = [x for x in (spot, cb) if x is not None]
        if fut is None or not cash_vals:
            return 0.0
        cash = sum(cash_vals) / len(cash_vals)
        return fut - cash

    fast_gap = gap(fast_ref)
    slow_gap = gap(slow_ref)
    accel = fast_gap - slow_gap

    if fast_gap >= 1.25:
        lead = "PERP_LED"
    elif fast_gap <= -0.75:
        lead = "CASH_LED"
    else:
        lead = "BALANCED"

    return {
        "version": VERSION,
        "status": "OK",
        "persistence": round(persistence, 4),
        "oppose_ratio": round(oppose_ratio, 4),
        "flow_mean": round(sum(means) / len(means), 4) if means else 0.0,
        "flow_mean_raw": round(sum(raw_means) / len(raw_means), 4) if raw_means else 0.0,
        "displacement_dominance": lead,
        "lead_gap_bps": round(fast_gap, 4),
        "lead_accel_bps": round(accel, 4),
        "freshness": fresh,
        "displacement_scope": "TRADE_RELATIVE_CASH_VS_PERP",
        "event_ordering_leader": "NOT_MEASURED",
        "event_ordering_authority": False,
        "spot_price_discovery": "NOT_MEASURED_HERE",
        "history_source": "ACTIVE_IGNITION_100MS",
        "policy": "CONTEXT_ONLY_TIME_ALIGNED_NO_SIGNAL_AUTHORITY",
    }
