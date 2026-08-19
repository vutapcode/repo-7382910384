"""Fast RAM-only microstructure validators for Tier-S entry quality."""

VERSION = "ENTRY_MICROSTRUCTURE_V3_CAUSAL_ONLY"


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
