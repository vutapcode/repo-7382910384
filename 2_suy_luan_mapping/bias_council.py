"""Adaptive causal cash-control Bias for the canonical Tier-S strategy.

Bias answers one question only: which direction currently owns meaningful
independent cash control, and is that control persisting, failing, or
transferring?

There is deliberately no fixed forecast horizon. Historical 15s..60m lenses
remain observation/replay diagnostics only. Live direction comes from the
ACTIVE causal cash wave: dual-independent-cash executed flow -> actual price
conversion -> persistence/failure/control transfer.

Directional authority:
- Binance Spot BTCUSDT executed cash + price.
- Coinbase BTC-USD executed cash + price.

Context only:
- Binance Futures price / executed flow.
- Open interest / funding / liquidation.
- L2/liquidity response until a named replay promotion exists.

One causal root counts once. Futures can explain urgency, build or unwind but
can never replace a missing independent cash venue. Fast entry timing and the
strict fast reversal proof remain owned by Ignition Core. Economics/fees remain
owned by Entry Edge; Bias only suppresses micro waves that have not established
persistent cash control.
"""

from collections import deque
import hashlib
from importlib import import_module
import json
import time


cash_wave_observation = import_module("2_suy_luan_mapping.cash_wave_observation")

VERSION = "BIAS_COUNCIL_V14_STABLE_ACQUISITION_WAVE"
CONTRACT = "DIRECTION_ONLY_NO_ENTRY_TIMING"
FORECAST_SCOPE = "MEANINGFUL_DIRECTIONAL_REGIME_NOT_FIXED_TIME_TARGET"
ACQUISITION_HANDOFF_VERSION = "CASH_CONTROL_ACQUISITION_HANDOFF_V1"

# Long lenses are historical diagnostics only. They never own live direction.
OBSERVATION_LENSES = (15.0, 60.0, 180.0, 600.0, 1800.0, 3600.0)
# Non-overlapping segments form the active causal-wave observation tape.
WAVE_SEGMENT_BOUNDARIES = (15.0, 60.0, 180.0, 600.0)
MAX_CONTEXT_SECONDS = 3900
SPOT_AGE = 3.0
CB_AGE = 5.0
FUT_AGE = 5.0
OI_AGE = 18.0  # overwritten only by the single-responsibility freshness hook
MIN_MOVE = 0.015  # percent; fallback market-noise floor, not an alpha target
FLOW_BALANCE_FLOOR = 0.05  # telemetry neutral band; never creates direction


def C(value):
    return max(0.0, min(1.0, float(value or 0.0)))


def vote(side="ABSTAIN", conf=0.0, reason="", **metrics):
    return {
        "vote": str(side or "ABSTAIN").upper(),
        "confidence": C(conf),
        "reason": str(reason or ""),
        "metrics": metrics,
    }


def fresh(ts, now, age):
    try:
        ts = float(ts or 0.0)
        return ts > 0.0 and 0.0 <= float(now) - ts <= float(age)
    except (TypeError, ValueError):
        return False


def mid(bid, ask):
    try:
        bid, ask = float(bid or 0.0), float(ask or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return (bid + ask) / 2.0 if bid > 0.0 and ask > bid else max(bid, ask)


def _pct(current, reference):
    current, reference = float(current or 0.0), float(reference or 0.0)
    return ((current - reference) / reference * 100.0) if current > 0.0 and reference > 0.0 else 0.0


def _imb(buy, sell):
    buy, sell = max(0.0, float(buy or 0.0)), max(0.0, float(sell or 0.0))
    total = buy + sell
    return (((buy - sell) / total) if total > 0.0 else 0.0), total


def thr(state, price):
    """Fallback noise floor only; direction still requires independent cash."""
    price = float(price or 0.0)
    atr = float(getattr(state, "atr_1m", 0.0) or 0.0)
    atr_pct = atr / price * 100.0 if price > 0.0 and atr > 0.0 else 0.0
    return max(MIN_MOVE, 0.15 * atr_pct)


def _epochs(row):
    return dict((row or {}).get("venue_epochs") or {})


def _same_epoch(current, reference, venue):
    left = int(_epochs(current).get(venue, 0) or 0)
    right = int(_epochs(reference).get(venue, 0) or 0)
    return left == right


def _price_move(current, reference, venue):
    if not _same_epoch(current, reference, venue):
        return None
    return _pct((current or {}).get(venue), (reference or {}).get(venue))


def price_vote(current, reference, threshold):
    """Compatibility diagnostic across venues; NEVER used for Bias authority."""
    if not current or not reference:
        return vote(reason="NO_PRICE_REFERENCE")
    moves, mismatched = {}, []
    for venue in ("spot", "coinbase", "futures"):
        value = _price_move(current, reference, venue)
        if value is None:
            mismatched.append(venue)
        elif float((current or {}).get(venue, 0.0) or 0.0) > 0.0 and float((reference or {}).get(venue, 0.0) or 0.0) > 0.0:
            moves[venue] = value
    long_names = [name for name, value in moves.items() if value >= threshold]
    short_names = [name for name, value in moves.items() if value <= -threshold]
    if len(long_names) >= 2:
        return vote("LONG", 0.60, "CROSS_VENUE_PRICE_DIAGNOSTIC", moves=moves, venues=long_names, epoch_mismatch=mismatched, authority=False)
    if len(short_names) >= 2:
        return vote("SHORT", 0.60, "CROSS_VENUE_PRICE_DIAGNOSTIC", moves=moves, venues=short_names, epoch_mismatch=mismatched, authority=False)
    return vote(reason="PRICE_NOT_ALIGNED", moves=moves, epoch_mismatch=mismatched, authority=False)


def cash_price_vote(current, reference, threshold):
    """Independent cash price acceptance. Futures can never satisfy this."""
    if not current or not reference:
        return vote(reason="NO_CASH_PRICE_REFERENCE")
    moves, mismatched = {}, []
    for venue in ("spot", "coinbase"):
        value = _price_move(current, reference, venue)
        if value is None:
            mismatched.append(venue)
        elif float((current or {}).get(venue, 0.0) or 0.0) > 0.0 and float((reference or {}).get(venue, 0.0) or 0.0) > 0.0:
            moves[venue] = value
    if mismatched:
        return vote(reason="CASH_PRICE_EPOCH_MISMATCH", moves=moves, epoch_mismatch=mismatched, authority=True)
    if len(moves) < 2:
        return vote(reason="INDEPENDENT_CASH_PRICE_INCOMPLETE", moves=moves, authority=True)
    if all(value >= threshold for value in moves.values()):
        return vote("LONG", 0.64, "DUAL_CASH_PRICE_ACCEPTANCE", moves=moves, venues=["spot", "coinbase"], authority=True)
    if all(value <= -threshold for value in moves.values()):
        return vote("SHORT", 0.64, "DUAL_CASH_PRICE_ACCEPTANCE", moves=moves, venues=["spot", "coinbase"], authority=True)
    return vote(reason="DUAL_CASH_PRICE_NOT_ALIGNED", moves=moves, authority=True)


def _cash_totals(state):
    return {
        "spot": {
            "buy": float(getattr(state, "spot_cvd_buy_total", 0.0) or 0.0),
            "sell": float(getattr(state, "spot_cvd_sell_total", 0.0) or 0.0),
        },
        "coinbase": {
            "buy": float(getattr(state, "coinbase_cvd_buy_total", 0.0) or 0.0),
            "sell": float(getattr(state, "coinbase_cvd_sell_total", 0.0) or 0.0),
        },
    }


def _flow_delta(current, reference, venue):
    if not current or not reference or not _same_epoch(current, reference, venue):
        return None
    cur = dict((current.get("cash_totals") or {}).get(venue) or {})
    ref = dict((reference.get("cash_totals") or {}).get(venue) or {})
    if not cur or not ref:
        return None
    buy = float(cur.get("buy", 0.0) or 0.0) - float(ref.get("buy", 0.0) or 0.0)
    sell = float(cur.get("sell", 0.0) or 0.0) - float(ref.get("sell", 0.0) or 0.0)
    if buy < -1e-9 or sell < -1e-9:
        return None
    imbalance, volume = _imb(max(0.0, buy), max(0.0, sell))
    return {"buy": max(0.0, buy), "sell": max(0.0, sell), "imbalance": imbalance, "volume": volume}


def cash_flow_vote(current, reference):
    """Executed dual-cash flow support; price remains the direction anchor."""
    rows = {}
    for venue in ("spot", "coinbase"):
        row = _flow_delta(current, reference, venue)
        if row is not None:
            rows[venue] = row
    if len(rows) < 2:
        return vote(reason="INDEPENDENT_CASH_FLOW_INCOMPLETE", venues=rows, authority="SUPPORT_ONLY")
    sides = {}
    for venue, row in rows.items():
        imbalance = float(row["imbalance"])
        sides[venue] = "LONG" if imbalance >= FLOW_BALANCE_FLOOR else "SHORT" if imbalance <= -FLOW_BALANCE_FLOOR else "BALANCED"
    if sides.get("spot") == sides.get("coinbase") and sides.get("spot") in ("LONG", "SHORT"):
        return vote(sides["spot"], 0.68, "DUAL_CASH_EXECUTED_FLOW", venues=rows, sides=sides, authority="SUPPORT_ONLY")
    if {sides.get("spot"), sides.get("coinbase")} == {"LONG", "SHORT"}:
        return vote(reason="DUAL_CASH_FLOW_CONFLICT", venues=rows, sides=sides, authority="SUPPORT_ONLY")
    return vote(reason="DUAL_CASH_FLOW_BALANCED_OR_MIXED", venues=rows, sides=sides, authority="SUPPORT_ONLY")


def flow_family_consensus(rows):
    """Only Binance Spot + Coinbase are independent directional cash roots."""
    rows = list(rows or ())
    by_name = {str(row[0]): row for row in rows if len(row) >= 2}
    spot, coinbase, futures = by_name.get("spot"), by_name.get("coinbase"), by_name.get("futures")
    if spot and coinbase and spot[1] == coinbase[1] and spot[1] in ("LONG", "SHORT"):
        return [spot, coinbase], "DUAL_CASH_FLOW"
    if spot and futures and not coinbase and spot[1] == futures[1]:
        return [], "BINANCE_COMPLEX_ECHO_UNCORROBORATED"
    if coinbase and futures and not spot and coinbase[1] == futures[1]:
        return [], "CASH_DERIVATIVE_NOT_DIRECTION_QUORUM"
    if spot and coinbase and spot[1] != coinbase[1]:
        return [], "INDEPENDENT_CASH_FLOW_CONFLICT"
    return [], "INSUFFICIENT_INDEPENDENT_CASH_FLOW"


def _legacy_flow(state, now, futures=False):
    buy = sell = 0.0
    cutoff = float(now) - 60.0
    name = "futures_flow_1s_buffer" if futures else "flow_1s_buffer"
    for row in list(getattr(state, name, ()) or ()):
        try:
            ts = float(row.get("ts", row.get("second", 0.0)) or 0.0)
            if cutoff <= ts <= float(now):
                buy += float(row.get("buy", 0.0) or 0.0)
                sell += float(row.get("sell", 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
    return _imb(buy, sell)


def flow_imb(state, now, fut=False):
    return _legacy_flow(state, now, futures=bool(fut))


def s1(current, slow, trigger, fast_ref, threshold):
    base = cash_price_vote(current, slow, threshold)
    confirm = cash_price_vote(current, trigger, threshold)
    fast = cash_price_vote(current, fast_ref, threshold)
    if base["vote"] in ("LONG", "SHORT") and confirm["vote"] == base["vote"]:
        return vote(base["vote"], 0.64, "DUAL_CASH_PRICE_PERSISTENCE", base=base, confirm=confirm, fast=fast, authority="CASH_ONLY")
    return vote(reason="DUAL_CASH_PRICE_NOT_PERSISTENT", base=base, confirm=confirm, fast=fast, fast_policy="FLIP_TELEMETRY_ONLY_NO_BIAS_ACQUISITION", authority="CASH_ONLY")


def _oi_observation(current, reference, threshold=0.0):
    if not current or not reference:
        return {"status": "UNKNOWN", "change_pct": None}
    now_oi = float(current.get("oi", 0.0) or 0.0)
    old_oi = float(reference.get("oi", 0.0) or 0.0)
    if now_oi <= 0.0 or old_oi <= 0.0:
        return {"status": "UNKNOWN", "change_pct": None}
    change = (now_oi - old_oi) / old_oi * 100.0
    eps = max(1e-9, float(threshold or 0.0))
    status = "EXPANDING" if change > eps else "CONTRACTING" if change < -eps else "STABLE"
    return {"status": status, "change_pct": change}


def s2(current, slow, threshold, macro_fresh=True, oi_context=None):
    """Positioning observation only. It never casts a direction vote."""
    if not macro_fresh:
        return vote(reason="OI_STALE_CONTEXT_ONLY", regime="OI_UNKNOWN", authority=False)
    cash = cash_price_vote(current, slow, threshold)
    if cash["reason"] == "CASH_PRICE_EPOCH_MISMATCH":
        return vote(reason="SPOT_PRICE_EPOCH_MISMATCH", regime="UNKNOWN", authority=False)
    oi = _oi_observation(current, oi_context or slow)
    side = cash.get("vote", "ABSTAIN")
    if side == "LONG" and oi["status"] == "EXPANDING":
        regime, hypothesis = "PRICE_UP_OI_EXPANSION", "POSITION_BUILD_CANDIDATE"
    elif side == "SHORT" and oi["status"] == "EXPANDING":
        regime, hypothesis = "PRICE_DOWN_OI_EXPANSION", "POSITION_BUILD_CANDIDATE"
    elif side == "LONG" and oi["status"] == "CONTRACTING":
        regime, hypothesis = "PRICE_UP_OI_CONTRACTION", "SHORT_COVERING_CANDIDATE"
    elif side == "SHORT" and oi["status"] == "CONTRACTING":
        regime, hypothesis = "PRICE_DOWN_OI_CONTRACTION", "LONG_LIQUIDATION_OR_CLOSE_CANDIDATE"
    else:
        regime, hypothesis = "POSITIONING_UNRESOLVED", "UNKNOWN"
    return vote(reason="POSITIONING_CONTEXT_ONLY", regime=regime, mechanism_hypothesis=hypothesis, mechanism_confirmed=False, oi=oi, cash_price_side=side, authority=False)


def s3(state, now):
    """60s compatibility view; only dual independent cash can support Bias."""
    spot_imb, spot_vol = _legacy_flow(state, now, futures=False)
    cb_vol = float(getattr(state, "coinbase_volume_1m", 0.0) or 0.0)
    cb_delta = float(getattr(state, "coinbase_cvd_1m", 0.0) or 0.0)
    cb_imb = cb_delta / cb_vol if cb_vol > 0.0 else 0.0
    rows = []
    if spot_vol > 0.0 and abs(spot_imb) >= FLOW_BALANCE_FLOOR:
        rows.append(("spot", "LONG" if spot_imb > 0.0 else "SHORT", abs(spot_imb), spot_vol, spot_imb))
    if cb_vol > 0.0 and abs(cb_imb) >= FLOW_BALANCE_FLOOR and fresh(getattr(state, "thoi_gian_coinbase_cuoi", 0.0), now, CB_AGE):
        rows.append(("coinbase", "LONG" if cb_imb > 0.0 else "SHORT", abs(cb_imb), cb_vol, cb_imb))
    fut_imb, fut_vol = _legacy_flow(state, now, futures=True)
    if fut_vol > 0.0 and abs(fut_imb) >= FLOW_BALANCE_FLOOR:
        rows.append(("futures", "LONG" if fut_imb > 0.0 else "SHORT", abs(fut_imb), fut_vol, fut_imb))
    agreed, family = flow_family_consensus(rows)
    metrics = [{"venue": row[0], "side": row[1], "imbalance": round(float(row[4]), 6), "volume_btc": round(float(row[3]), 6)} for row in rows]
    if not agreed:
        return vote(reason=family, venues=metrics, evidence_family=family, authority="CASH_ONLY")
    return vote(agreed[0][1], 0.68, "DUAL_CASH_EXECUTED_FLOW", venues=metrics, evidence_family=family, authority="SUPPORT_ONLY")


def story(votes):
    price = str((votes.get("S1_cross_price") or {}).get("vote", "ABSTAIN"))
    flow = str((votes.get("S3_multi_flow") or {}).get("vote", "ABSTAIN"))
    if price in ("LONG", "SHORT") and flow == price:
        return "CASH_PRICE_AND_EXECUTED_FLOW_CONTROL", price, 0.0, False
    if price in ("LONG", "SHORT"):
        return "CASH_PRICE_CONTROL_FLOW_UNRESOLVED", price, 0.0, False
    return "MIXED_OR_INCOMPLETE", "ABSTAIN", 0.0, False


def combine(votes, story_tuple):
    """Compatibility adapter: price owns direction; no weighted council."""
    price = votes.get("S1_cross_price") or vote(reason="MISSING_CASH_PRICE")
    flow = votes.get("S3_multi_flow") or vote(reason="MISSING_CASH_FLOW")
    side = str(price.get("vote", "ABSTAIN"))
    if side not in ("LONG", "SHORT"):
        return "ABSTAIN", 0.0, 0, str(price.get("reason") or "NO_CASH_CONTROL"), 0.0, 0.0
    flow_side = str(flow.get("vote", "ABSTAIN"))
    if flow_side in ("LONG", "SHORT") and flow_side != side:
        return "ABSTAIN", 0.0, 1, "DUAL_CASH_PRICE_FLOW_DIVERGENCE", 0.0, 0.0
    confidence = 0.70 if flow_side == side else 0.62
    return side, confidence, 1, story_tuple[0], confidence if side == "LONG" else 0.0, confidence if side == "SHORT" else 0.0


def _reference(buckets, target, tolerance):
    best, distance = None, None
    for key, row in (buckets or {}).items():
        d = abs(float(key) - float(target))
        if d <= tolerance and (distance is None or d < distance):
            best, distance = row, d
    return best


def bucket_ref(buckets, target, tolerance):
    return _reference(buckets, target, tolerance)


def bias_buckets(state, now, legacy_history=None):
    """One bounded second-level tape of price + cumulative executed cash."""
    second = int(float(now))
    buckets = getattr(state, "bias_price_buckets", None)
    if not isinstance(buckets, dict):
        buckets = {}
        state.bias_price_buckets = buckets
        for row in list(legacy_history or ()):
            try:
                buckets[int(float(row.get("ts", 0.0)))] = dict(row)
            except (AttributeError, TypeError, ValueError):
                continue
    last = float(getattr(state, "_bias_bucket_last_sample_at", 0.0) or 0.0)
    if last > 0.0 and float(now) - last > max(SPOT_AGE, CB_AGE):
        previous = len(buckets)
        buckets.clear()
        state.bias_history_reset_reason = "BIAS_LOOP_GAP"
        state.bias_history_previous_coverage_seconds = previous
        state.bias_history_reset_count = int(getattr(state, "bias_history_reset_count", 0) or 0) + 1
    state._bias_bucket_last_sample_at = float(now)
    cutoff = second - MAX_CONTEXT_SECONDS
    for key in list(buckets):
        if int(key) < cutoff:
            buckets.pop(key, None)
    return buckets


def _lens_reports(current, buckets, state, now, threshold):
    """Historical diagnostics only; outputs have zero live direction authority."""
    reports = []
    for seconds in OBSERVATION_LENSES:
        tolerance = max(2.0, min(10.0, seconds * 0.04))
        ref = _reference(buckets, float(now) - seconds, tolerance)
        if not ref:
            continue
        reports.append({
            "seconds": seconds,
            "price": cash_price_vote(current, ref, threshold),
            "flow": cash_flow_vote(current, ref),
            "reference_ts": ref.get("ts"),
            "authority": False,
        })
    return reports


def _segment_reports(current, buckets, now, threshold):
    """Build non-overlapping newest->oldest segments for causal-wave inference."""
    references = []
    for seconds in WAVE_SEGMENT_BOUNDARIES:
        tolerance = max(2.0, min(10.0, seconds * 0.04))
        ref = _reference(buckets, float(now) - seconds, tolerance)
        if ref:
            references.append((float(seconds), ref))
    segments = []
    newer = current
    start_age = 0.0
    for end_age, older in references:
        segments.append({
            "start_age_seconds": start_age,
            "end_age_seconds": end_age,
            "price": cash_price_vote(newer, older, threshold),
            "flow": cash_flow_vote(newer, older),
            "newer_ts": newer.get("ts"),
            "older_ts": older.get("ts"),
            "newer_epochs": _epochs(newer),
            "older_epochs": _epochs(older),
            "newer_prices": {
                name: float(newer.get(name, 0.0) or 0.0)
                for name in ("spot", "coinbase", "futures")
            },
            "older_prices": {
                name: float(older.get(name, 0.0) or 0.0)
                for name in ("spot", "coinbase", "futures")
            },
        })
        newer = older
        start_age = end_age
    return segments


def _canonical_hash(payload):
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact_acquisition_segment(segment):
    """Freeze only evidence that established temporal cash persistence."""
    segment = dict(segment or {})
    return {
        "state": str(segment.get("state") or "UNKNOWN"),
        "side": str(segment.get("side") or "ABSTAIN").upper(),
        "reason": str(segment.get("reason") or "UNKNOWN"),
        "start_age_seconds": segment.get("start_age_seconds"),
        "end_age_seconds": segment.get("end_age_seconds"),
        "newer_ts": segment.get("newer_ts"),
        "older_ts": segment.get("older_ts"),
        "newer_epochs": dict(segment.get("newer_epochs") or {}),
        "older_epochs": dict(segment.get("older_epochs") or {}),
        "price": dict(segment.get("price") or {}),
        "flow": dict(segment.get("flow") or {}),
    }


def _seal_acquisition_handoff(report, completed_at):
    """Seal one neutral-to-owned cash wave; never grant Entry by itself."""
    report = dict(report or {})
    cash = dict(report.get("cash_control") or {})
    side = str(report.get("bias") or "ABSTAIN").upper()
    segments = [
        dict(row) for row in cash.get("segments") or ()
        if str((row or {}).get("state") or "").upper() == "CONVERTING"
        and str((row or {}).get("side") or "").upper() == side
    ][:2]
    if (
        side not in ("LONG", "SHORT")
        or str(report.get("hysteresis") or "") != "ACQUIRE_CASH_REGIME"
        or str(cash.get("wave_state") or "").upper() != "CONTROLLED"
        or not bool(cash.get("meaningful_for_action"))
        or len(segments) < 2
    ):
        return None
    latest, previous = segments[0], segments[1]
    epochs = dict(latest.get("newer_epochs") or {})
    if not all(name in epochs for name in ("spot", "coinbase")):
        return None
    onset_s = float(previous.get("older_ts") or 0.0)
    completed_s = float(completed_at or 0.0)
    if onset_s <= 0.0 or completed_s <= onset_s:
        return None
    evidence = [
        _compact_acquisition_segment(previous),
        _compact_acquisition_segment(latest),
    ]
    sealed_payload = {
        "version": ACQUISITION_HANDOFF_VERSION,
        "side": side,
        "first_converting_segment_onset_ms": int(onset_s * 1000.0),
        "ownership_completed_ms": int(completed_s * 1000.0),
        "venue_epochs": {
            "spot": int(epochs.get("spot", 0) or 0),
            "coinbase": int(epochs.get("coinbase", 0) or 0),
        },
        "directional_cash_roots": [
            "BINANCE_SPOT_CASH", "COINBASE_USD_CASH",
        ],
        "temporal_persistence_segments": 2,
        "segment_evidence": evidence,
        "bias_version": VERSION,
        "bias_confidence": float(report.get("confidence", 0.0) or 0.0),
        "bias_hysteresis": str(report.get("hysteresis") or "UNKNOWN"),
    }
    seal = _canonical_hash(sealed_payload)
    return {
        **sealed_payload,
        "causal_wave_id": "cash-acquisition:%s" % seal[:20],
        "handoff_hash": seal,
        "sealed_payload": sealed_payload,
        "status": "SEALED",
        "sealed": True,
        "authority": False,
        "entry_authority": False,
        "policy": (
            "BIAS_OWNS_DIRECTION_IGNITION_MUST_REPROVE_CURRENT_TIMING_"
            "TEMPORAL_PERSISTENCE_IS_NOT_A_NEW_INDEPENDENT_ROOT"
        ),
    }


def _same_acquisition_wave(existing, candidate):
    """Same cash roots, epochs and overlapping evidence interval means one wave."""
    existing = dict(existing or {})
    candidate = dict(candidate or {})
    if str(existing.get("status") or "") != "SEALED":
        return False
    if str(existing.get("side") or "") != str(candidate.get("side") or ""):
        return False
    if dict(existing.get("venue_epochs") or {}) != dict(
        candidate.get("venue_epochs") or {}
    ):
        return False
    old_start = int(existing.get("first_converting_segment_onset_ms", 0) or 0)
    old_end = int(existing.get("ownership_completed_ms", 0) or 0)
    new_start = int(candidate.get("first_converting_segment_onset_ms", 0) or 0)
    new_end = int(candidate.get("ownership_completed_ms", 0) or 0)
    return bool(
        old_start > 0 and new_start > 0
        and max(old_start, new_start) <= min(old_end, new_end)
    )


def _terminate_acquisition_wave(existing, report, completed_at):
    """Terminate only on an observed causal falsifier, never on mere UNKNOWN."""
    existing = dict(existing or {})
    if str(existing.get("status") or "") != "SEALED":
        return existing
    cash = dict((report or {}).get("cash_control") or {})
    wave_state = str(
        cash.get("wave_state") or cash.get("regime_state") or "UNKNOWN"
    ).upper()
    current_side = str((report or {}).get("bias") or "ABSTAIN").upper()
    old_side = str(existing.get("side") or "ABSTAIN").upper()
    reason = None
    if wave_state in {"CONTRADICTED", "ABSORPTION", "EXHAUSTION"}:
        reason = str(cash.get("falsifier") or wave_state)
    elif wave_state == "CONTROL_TRANSFER" and current_side in {
        "LONG", "SHORT",
    } and current_side != old_side:
        reason = "OPPOSITE_CASH_CONTROL_TRANSFER"
    if reason is None:
        return existing
    existing.update({
        "status": "TERMINATED_CAUSAL_FALSIFIER",
        "terminated_at_ms": int(float(completed_at) * 1000.0),
        "termination_reason": reason,
    })
    return existing


def _adaptive_cash_regime(reports):
    """Legacy lens decoder retained for replay/tests; not used by live evaluate."""
    valid = [row for row in reports if (row.get("price") or {}).get("vote") in ("LONG", "SHORT")]
    if not valid:
        return {
            "raw_side": "ABSTAIN", "regime_state": "UNKNOWN",
            "phase": "WARMUP_OR_NEUTRAL", "context_side": "ABSTAIN",
            "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
            "dominant_lens_seconds": None, "flow_support": "ABSTAIN",
        }
    valid.sort(key=lambda row: float(row["seconds"]))
    near, context = valid[0], valid[-1]
    near_side = str(near["price"]["vote"])
    context_side = str(context["price"]["vote"])
    same = [row for row in valid if str(row["price"]["vote"]) == context_side]
    recent = [row for row in valid if row["seconds"] <= 180.0]
    dual_flow_side = next((
        str(row["flow"]["vote"]) for row in recent
        if str((row.get("flow") or {}).get("vote")) in ("LONG", "SHORT")
    ), "ABSTAIN")
    transfer = bool(
        near_side != context_side
        and len(recent) >= 2
        and all(str(row["price"]["vote"]) == near_side for row in recent)
        and dual_flow_side == near_side
    )
    if transfer:
        return {
            "raw_side": near_side, "regime_state": "CONTROL_TRANSFER",
            "phase": "REVERSAL_CANDIDATE", "context_side": context_side,
            "candidate_side": near_side, "control_transfer_confirmed": True,
            "dominant_lens_seconds": max(row["seconds"] for row in recent),
            "flow_support": dual_flow_side,
        }
    if near_side != context_side:
        return {
            "raw_side": context_side, "regime_state": "PULLBACK",
            "phase": "PULLBACK_AGAINST_CONTEXT", "context_side": context_side,
            "candidate_side": near_side, "control_transfer_confirmed": False,
            "dominant_lens_seconds": context["seconds"],
            "flow_support": dual_flow_side,
        }
    if len(same) >= 2 or float(context["seconds"]) >= 60.0:
        return {
            "raw_side": context_side, "regime_state": "ESTABLISHED",
            "phase": "ESTABLISHED_TREND", "context_side": context_side,
            "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
            "dominant_lens_seconds": context["seconds"],
            "flow_support": dual_flow_side,
        }
    return {
        "raw_side": near_side, "regime_state": "EMERGING",
        "phase": "CONTEXT_WITHOUT_CONFIRMATION", "context_side": near_side,
        "candidate_side": "ABSTAIN", "control_transfer_confirmed": False,
        "dominant_lens_seconds": near["seconds"],
        "flow_support": dual_flow_side,
    }


def direction_memory(current, context, slow, trigger, threshold):
    """Legacy signature retained for tests/replay decoders."""
    context_vote = cash_price_vote(current, context, threshold) if context else vote(reason="NO_CONTEXT")
    slow_vote = cash_price_vote(current, slow, threshold) if slow else vote(reason="NO_SLOW")
    trigger_vote = cash_price_vote(current, trigger, threshold) if trigger else vote(reason="NO_TRIGGER")
    context_side = str(context_vote.get("vote", "ABSTAIN"))
    trigger_side = str(trigger_vote.get("vote", "ABSTAIN"))
    if context_side in ("LONG", "SHORT") and trigger_side in ("LONG", "SHORT") and trigger_side != context_side:
        phase, candidate = "PULLBACK_AGAINST_CONTEXT", trigger_side
    elif context_side in ("LONG", "SHORT") and slow_vote.get("vote") == context_side:
        phase, candidate = "ESTABLISHED_TREND", "ABSTAIN"
    elif context_side in ("LONG", "SHORT"):
        phase, candidate = "CONTEXT_WITHOUT_CONFIRMATION", "ABSTAIN"
    else:
        phase, candidate = "WARMUP_OR_NEUTRAL", "ABSTAIN"
    return {
        "context_side": context_side, "candidate_side": candidate, "phase": phase,
        "cash_180s": context_vote, "cash_60s": slow_vote, "cash_15s": trigger_vote,
    }


def _compat_confidence(regime, flow_side, raw_side):
    """Compatibility metadata only; never interpreted as a probability."""
    if raw_side not in ("LONG", "SHORT"):
        return 0.0
    if regime in ("CONTROL_TRANSFER",):
        return 0.72
    if regime in ("CONTROLLED", "ESTABLISHED"):
        return 0.70
    if regime == "PULLBACK":
        return 0.60
    if regime in ("EMERGING_CONTROL", "EMERGING"):
        return 0.52
    return 0.0


def _hyst(state, report):
    """Evidence-driven state transition. No elapsed-time flip rule exists."""
    raw = str(report.get("bias", "ABSTAIN") or "ABSTAIN").upper()
    raw_conf = float(report.get("confidence", 0.0) or 0.0)
    old = str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
    old_conf = float(getattr(state, "bias_confidence", 0.0) or 0.0)
    memory = dict(report.get("direction_memory") or {})
    phase = str(memory.get("phase", ""))
    context = str(memory.get("context_side", "ABSTAIN"))
    cash = dict(report.get("cash_control") or {})
    source = str(report.get("knowledge_state", "UNKNOWN_MARKET"))

    if source == "UNKNOWN_SOURCE":
        return "ABSTAIN", 0.0, "INDEPENDENT_CASH_SOURCE_UNKNOWN"
    if source == "DIVERGING":
        return "ABSTAIN", 0.0, "INDEPENDENT_CASH_EVIDENCE_DIVERGING"
    if old not in ("LONG", "SHORT"):
        wave_state = str(
            cash.get("wave_state") or cash.get("regime_state") or "UNKNOWN"
        ).upper()
        meaningful = bool(cash.get("meaningful_for_action"))
        if (
            raw in ("LONG", "SHORT")
            and wave_state == "CONTROLLED"
            and meaningful
        ):
            return raw, raw_conf, "ACQUIRE_CASH_REGIME"
        if raw in ("LONG", "SHORT") and wave_state == "EMERGING_CONTROL":
            return "ABSTAIN", 0.0, "OBSERVE_EMERGING_CASH_CONTROL"
        return "ABSTAIN", 0.0, "NO_ACTIONABLE_CASH_REGIME"
    if phase == "PULLBACK_AGAINST_CONTEXT" and context == old and raw == old:
        return old, max(0.55, min(old_conf, raw_conf or old_conf)), "HOLD_CONTEXT_PULLBACK"
    if phase == "REVERSAL_CANDIDATE":
        if bool(cash.get("control_transfer_confirmed")) and raw in ("LONG", "SHORT") and raw != old:
            return raw, raw_conf, "EVIDENCE_CONFIRMED_CONTROL_TRANSFER"
        return "ABSTAIN", 0.0, "RELEASE_DURING_UNPROVEN_CONTROL_TRANSFER"
    if raw == old:
        return old, raw_conf, "STABLE_CASH_CONTROL"
    if raw == "ABSTAIN" and context == old and phase in ("ESTABLISHED_TREND", "CONTEXT_WITHOUT_CONFIRMATION"):
        return old, max(0.55, old_conf * 0.92), "HOLD_CONTEXT_THROUGH_ABSTAIN"
    return "ABSTAIN", 0.0, "CONFLICT_RELEASE_TO_NEUTRAL"


def flow_question_context(state, now, flow_vote=None):
    spot, spot_vol = _legacy_flow(state, now, futures=False)
    fut, fut_vol = _legacy_flow(state, now, futures=True)
    cb3_vol = float(getattr(state, "coinbase_volume_3s", 0.0) or 0.0)
    cb3 = float(getattr(state, "coinbase_cvd_3s", 0.0) or 0.0) / cb3_vol if cb3_vol > 0.0 else 0.0
    marginal = "LONG" if cb3 >= FLOW_BALANCE_FLOOR else "SHORT" if cb3 <= -FLOW_BALANCE_FLOOR else "ABSTAIN"
    background = str((flow_vote or {}).get("vote", "ABSTAIN"))
    return {
        "background_pressure_60s": {"side": background, "authority": "SUPPORT_ONLY"},
        "marginal_control_1_5s": {"side": marginal, "authority": False},
        "causal_families": {
            "cash_family": {"dual_cash_side": background if background in ("LONG", "SHORT") else "UNKNOWN"},
            "binance_complex_echo": bool(spot_vol > 0.0 and fut_vol > 0.0),
        },
    }


def fut_price(state, now):
    rows = getattr(state, "danh_sach_khop_lenh_futures", ()) or ()
    try:
        row = rows[-1]
        ts = float(row.get("thoi_gian_ms", 0.0) or 0.0) / 1000.0
        px = float(row.get("gia", 0.0) or 0.0)
        if px > 0.0 and fresh(ts, now, FUT_AGE):
            return px, "FUTURES_AGGTRADE"
    except (IndexError, AttributeError, TypeError, ValueError):
        pass
    return 0.0, "UNAVAILABLE"


def _previous_wave_side(state):
    side = str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
    wave_state = str(getattr(state, "bias_wave_state", "") or "").upper()
    if wave_state in {"CONTROLLED", "PULLBACK"} and side in {"LONG", "SHORT"}:
        return side
    # Upgrade/restart compatibility: an old persisted Bias may predate V12.
    if not wave_state and side in {"LONG", "SHORT"} and float(getattr(state, "bias_confidence", 0.0) or 0.0) >= 0.55:
        return side
    return "ABSTAIN"


def evaluate(state, now=None, force_full=False):
    now = time.time() if now is None else float(now)
    spot_fresh = fresh(getattr(state, "thoi_gian_tick_cuoi", 0.0), now, SPOT_AGE)
    cb_ts = float(getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0) or 0.0) or float(getattr(state, "thoi_gian_coinbase_cuoi", 0.0) or 0.0)
    cb_fresh = fresh(cb_ts, now, CB_AGE)
    spot = mid(getattr(state, "best_bid", 0.0), getattr(state, "best_ask", 0.0)) if spot_fresh else 0.0
    coinbase = float(getattr(state, "coinbase_price", 0.0) or 0.0) if cb_fresh else 0.0
    futures, fut_source = fut_price(state, now)
    macro_fresh = fresh(getattr(state, "thoi_gian_vi_mo_cuoi", 0.0), now, OI_AGE)
    oi = float(getattr(state, "open_interest", 0.0) or 0.0) if macro_fresh else 0.0

    buckets = bias_buckets(state, now, getattr(state, "bias_price_history", None))
    current = {
        "ts": now, "spot": spot, "coinbase": coinbase, "futures": futures, "oi": oi,
        "cash_totals": _cash_totals(state),
        "venue_epochs": {
            "spot": int(getattr(state, "spot_flow_epoch", 0) or 0),
            "coinbase": int(getattr(state, "coinbase_flow_epoch", 0) or 0),
            "futures": int(getattr(state, "futures_flow_epoch", 0) or 0),
        },
    }
    buckets[int(now)] = current
    threshold = thr(state, spot)
    lenses = _lens_reports(current, buckets, state, now, threshold) if spot_fresh and cb_fresh else []
    segments = _segment_reports(current, buckets, now, threshold) if spot_fresh and cb_fresh else []
    wave = cash_wave_observation.infer(
        segments,
        previous_side=_previous_wave_side(state),
        # Live L2 is intentionally not promoted here. The observation owner can
        # consume execution-linked liquidity in matched replay without allowing
        # raw walls/cancels to create direction.
        liquidity=(),
    )

    slow = _reference(buckets, now - 60.0, 3.0)
    trigger = _reference(buckets, now - 15.0, 2.0)
    context = _reference(buckets, now - 180.0, 7.0)
    fast_ref = _reference(buckets, now - 4.0, 1.5)
    oi_context = _reference(buckets, now - 300.0, 20.0)
    s1_vote = s1(current, slow, trigger, fast_ref, threshold) if spot_fresh and cb_fresh else vote(reason="INDEPENDENT_CASH_SOURCE_STALE")
    s2_vote = s2(current, slow, threshold, macro_fresh, oi_context) if slow else vote(reason="POSITIONING_CONTEXT_WARMUP", authority=False)
    s3_vote = s3(state, now)

    raw_side = str(wave.get("raw_side") or "ABSTAIN")
    wave_state = str(wave.get("wave_state") or "UNKNOWN")
    newest_flow_side = str((((wave.get("segments") or [{}])[0]).get("flow_side") if wave.get("segments") else "ABSTAIN") or "ABSTAIN")
    raw_conf = _compat_confidence(wave_state, newest_flow_side, raw_side)
    if not spot_fresh or not cb_fresh:
        raw_side, raw_conf = "ABSTAIN", 0.0
        knowledge = "UNKNOWN_SOURCE"
        reason = "INDEPENDENT_CASH_SOURCE_STALE"
    elif wave_state == "CONTRADICTED":
        knowledge = "DIVERGING"
        reason = "DUAL_CASH_FLOW_PRICE_CONTRADICTION"
    elif wave_state == "UNKNOWN":
        knowledge = "UNKNOWN_MARKET"
        reason = "ACTIVE_CASH_WAVE_UNRESOLVED"
    else:
        knowledge = "SUPPORTED"
        reason = {
            "CONTROLLED": "ACTIVE_DUAL_CASH_WAVE_CONTROL",
            "PULLBACK": "PULLBACK_INSIDE_ACTIVE_CASH_WAVE",
            "CONTROL_TRANSFER": "DUAL_CASH_WAVE_CONTROL_TRANSFER",
            "EMERGING_CONTROL": "EMERGING_DUAL_CASH_WAVE",
            "EXHAUSTION": "OLD_CASH_WAVE_EXHAUSTED",
            "ABSORPTION": "OLD_CASH_WAVE_ABSORBED",
            "TRANSITION": "CASH_CONTROL_TRANSFER_UNPROVEN",
        }.get(wave_state, "ACTIVE_CASH_WAVE_OBSERVED")

    direction_mem = {
        "context_side": wave.get("context_side", "ABSTAIN"),
        "candidate_side": wave.get("candidate_side", "ABSTAIN"),
        "phase": wave.get("phase", "WARMUP_OR_NEUTRAL"),
        "cash_180s": next(((row.get("price") or {}) for row in lenses if row["seconds"] == 180.0), vote(reason="UNAVAILABLE")),
        "cash_60s": next(((row.get("price") or {}) for row in lenses if row["seconds"] == 60.0), vote(reason="UNAVAILABLE")),
        "cash_15s": next(((row.get("price") or {}) for row in lenses if row["seconds"] == 15.0), vote(reason="UNAVAILABLE")),
        "historical_lens_direction_authority": False,
    }
    cash_control = {
        **wave,
        "regime_state": wave_state,
        "observation_segments": segments,
        "observation_lenses": lenses,
        "authority_roots": ["BINANCE_SPOT_CASH", "COINBASE_USD_CASH"],
        "futures_direction_authority": False,
        "oi_direction_authority": False,
        "historical_lens_direction_authority": False,
        "static_l2_direction_authority": False,
        "flow_price_conversion_required": True,
        "micro_wave_entry_authority": False,
    }
    st = story({"S1_cross_price": s1_vote, "S2_price_x_oi": s2_vote, "S3_multi_flow": s3_vote})
    raw_report = {
        "version": VERSION, "bias": raw_side, "confidence": raw_conf,
        "quorum": 1 if raw_side in ("LONG", "SHORT") else 0,
        "reason": reason, "mode": "FULL", "regime_state": wave_state,
        "wave_state": wave_state,
        "knowledge_state": knowledge, "forecast_scope": FORECAST_SCOPE,
        "dominant_lens_seconds": None,
        "story": {"name": st[0], "direction": st[1], "confidence_adjustment": 0.0, "veto": False},
        "s_votes": {"S1_cross_price": s1_vote, "S2_price_x_oi": s2_vote, "S3_multi_flow": s3_vote},
        "a_votes": {"A1_funding_basis": vote(reason="CONTEXT_ONLY"), "A2_spot_lead": vote(reason="CONTEXT_ONLY")},
        "direction_scores": {"long": raw_conf if raw_side == "LONG" else 0.0, "short": raw_conf if raw_side == "SHORT" else 0.0, "margin": raw_conf},
        "flow_question_context": flow_question_context(state, now, s3_vote),
        "direction_memory": direction_mem,
        "cash_control": cash_control,
        "derivative_context": {
            "oi": dict((s2_vote.get("metrics") or {})),
            "futures_price": futures or None,
            "futures_price_source": fut_source,
            "authority": False,
        },
        "freshness": {"spot": spot_fresh, "coinbase": cb_fresh, "futures": futures > 0.0, "oi_macro": macro_fresh},
        "contract": CONTRACT, "futures_price_source": fut_source, "ts": now,
    }
    side, conf, transition_reason = _hyst(state, raw_report)
    out = dict(raw_report)
    out.update(raw_bias=raw_side, raw_confidence=raw_conf, bias=side, confidence=round(C(conf), 6), hysteresis=transition_reason)
    out["reversal_latch"] = {
        "status": "CONFIRMED" if transition_reason == "EVIDENCE_CONFIRMED_CONTROL_TRANSFER" else "PENDING" if wave.get("phase") == "REVERSAL_CANDIDATE" else "INACTIVE",
        "candidate_side": wave.get("candidate_side", "ABSTAIN"),
        "started_at": None,
        "confirmed_at": now if transition_reason == "EVIDENCE_CONFIRMED_CONTROL_TRANSFER" else None,
        "authority": False,
        "policy": "EVIDENCE_DRIVEN_NO_TIMER_IGNITION_OWNS_FAST_CAUSAL_EPISODE",
    }
    return out


def update_state(state, now=None, force_full=False):
    previous_side = str(
        getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN"
    ).upper()
    out = evaluate(state, now=now, force_full=force_full)
    completed_at = float(out.get("ts", time.time()) or time.time())
    existing = _terminate_acquisition_wave(
        getattr(state, "bias_acquisition_handoff", {}), out, completed_at,
    )
    if existing:
        state.bias_acquisition_handoff = existing
    handoff = None
    if previous_side not in ("LONG", "SHORT") and out.get("bias") in (
        "LONG", "SHORT",
    ):
        candidate = _seal_acquisition_handoff(out, completed_at)
        if candidate is not None:
            if _same_acquisition_wave(existing, candidate):
                handoff = dict(existing)
                handoff["last_observed_at_ms"] = int(completed_at * 1000.0)
                handoff["reacquisition_observations"] = int(
                    handoff.get("reacquisition_observations", 0) or 0
                ) + 1
            else:
                handoff = candidate
            state.bias_acquisition_handoff = handoff
    existing = dict(
        handoff or getattr(state, "bias_acquisition_handoff", {}) or {}
    )
    if existing:
        current_epochs = {
            "spot": int(getattr(state, "spot_flow_epoch", 0) or 0),
            "coinbase": int(getattr(state, "coinbase_flow_epoch", 0) or 0),
        }
        epoch_changed = any(
            int((existing.get("venue_epochs") or {}).get(name, 0) or 0)
            != int(current_epochs.get(name, 0) or 0)
            for name in ("spot", "coinbase")
        )
        if epoch_changed:
            existing["status"] = "INVALIDATED_EPOCH_CHANGE"
            existing["invalidated_at_ms"] = int(completed_at * 1000.0)
            state.bias_acquisition_handoff = existing
        out["acquisition_handoff"] = dict(existing)
    state.bias_state = out["bias"]
    state.bias_confidence = out["confidence"]
    state.bias_wave_state = out.get("wave_state", "UNKNOWN")
    state.bias_council = out
    state.bias_updated_at = out["ts"]
    state.bias_version = VERSION
    state.macro_bias = "NEUTRAL"
    return out
