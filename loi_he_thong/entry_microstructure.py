"""Fast RAM-only microstructure validators for Tier-S entry quality."""

VERSION = "ENTRY_MICROSTRUCTURE_V2_BOOK"


def _votes(result):
    return (result or {}).get("s_votes") or {}


def _price(result):
    seat = _votes(result).get("S1_cross_venue_price_acceptance") or {}
    return seat.get("metrics") or {}


def _flow(result):
    seat = _votes(result).get("S2_multi_venue_executed_flow") or {}
    return seat.get("metrics") or {}


def price_impact(result):
    """Detect strong executed flow that fails to convert into cash-market price."""
    pm, fm = _price(result), _flow(result)
    moves, venues = pm.get("moves") or {}, fm.get("venues") or {}
    threshold = float((result or {}).get("price_threshold_bps") or 0.0)
    cash = [max(0.0, float(moves.get(v) or 0.0)) for v in ("spot", "coinbase") if v in moves]
    flows = [max(0.0, float((r or {}).get("signed_imbalance") or 0.0)) for r in venues.values()]
    supporters = len(fm.get("supporters") or ())
    cash_impact = max(cash) if cash else 0.0
    flow_strength = sum(flows) / len(flows) if flows else 0.0
    absorbed = bool(
        supporters >= 2 and flow_strength >= 0.18 and threshold > 0.0
        and cash_impact < threshold * 0.70
    )
    efficient = bool(
        supporters >= 2 and threshold > 0.0 and cash_impact >= threshold
        and flow_strength >= 0.10
    )
    return {
        "status": "ABSORBED" if absorbed else ("PASS" if efficient else "NEUTRAL"),
        "cash_impact_bps": round(cash_impact, 4),
        "flow_strength": round(flow_strength, 4),
        "flow_supporters": supporters,
        "absorbed": absorbed,
        "efficient": efficient,
    }


def spot_perp_basis(result):
    """Reject only extreme perp-led expansion; normal lead remains advisory."""
    moves = _price(result).get("moves") or {}
    threshold = float((result or {}).get("price_threshold_bps") or 0.0)
    futures = float(moves.get("futures") or 0.0)
    cash = [float(moves[v]) for v in ("spot", "coinbase") if v in moves]
    cash_best = max(cash) if cash else 0.0
    cash_supporters = sum(1 for x in cash if threshold > 0.0 and x >= threshold)
    lead = futures - cash_best
    limit = max(3.0, threshold * 2.5)
    expansion = bool(
        threshold > 0.0 and futures > 0.0 and lead >= limit and cash_supporters < 2
    )
    return {
        "status": "PERP_EXPANSION" if expansion else ("CASH_CONFIRMED" if cash_supporters >= 2 else "NEUTRAL"),
        "futures_move_bps": round(futures, 4),
        "cash_best_move_bps": round(cash_best, 4),
        "lead_bps": round(lead, 4),
        "limit_bps": round(limit, 4),
        "cash_supporters": cash_supporters,
        "perp_expansion": expansion,
    }


def _qty(levels, n=3):
    total = 0.0
    for row in list(levels or ())[:n]:
        try:
            total += float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
    return total


def book_resiliency(state, result):
    """Read refill/depletion, not wall size. Advisory only: book alone must never veto."""
    now = float((result or {}).get("ts") or 0.0)
    rows = list(getattr(state, "hang_doi_so_lenh", ()) or ())
    if now <= 0.0 or len(rows) < 2:
        return {"status": "UNKNOWN", "adverse_refill": False, "supportive": False}

    cur = rows[-1]
    cur_ts = float(cur.get("timestamp") or 0.0)
    if cur_ts <= 0.0 or now - cur_ts > 1.0:
        return {"status": "STALE", "adverse_refill": False, "supportive": False}

    prior = None
    for row in reversed(rows[:-1]):
        age = cur_ts - float(row.get("timestamp") or 0.0)
        if 0.25 <= age <= 1.25:
            prior = row
            break
    if prior is None:
        return {"status": "WARMUP", "adverse_refill": False, "supportive": False}

    cb, ca = _qty(cur.get("bids")), _qty(cur.get("asks"))
    pb, pa = _qty(prior.get("bids")), _qty(prior.get("asks"))
    if min(cb, ca, pb, pa) <= 0.0:
        return {"status": "INVALID", "adverse_refill": False, "supportive": False}

    bid_ratio, ask_ratio = cb / pb, ca / pa
    side = str((result or {}).get("side") or "").upper()
    support = bid_ratio if side == "LONG" else ask_ratio
    resistance = ask_ratio if side == "LONG" else bid_ratio
    adverse = resistance >= 1.45 and support <= 0.75
    supportive = support >= 1.10 and resistance <= 1.10
    return {
        "status": "ADVERSE_REFILL" if adverse else ("SUPPORTIVE" if supportive else "NEUTRAL"),
        "bid_refill_ratio": round(bid_ratio, 4),
        "ask_refill_ratio": round(ask_ratio, 4),
        "support_ratio": round(support, 4),
        "resistance_ratio": round(resistance, 4),
        "window_s": round(cur_ts - float(prior.get("timestamp") or 0.0), 4),
        "adverse_refill": adverse,
        "supportive": supportive,
    }
