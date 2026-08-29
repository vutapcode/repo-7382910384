"""Volume- and venue-prior-weighted flow for Tier-S persistence context.

Static venue priors are not feed-health scores. Dynamic freshness/alignment is
reported separately by the flow displacement engine and remains fail-closed.
"""
import math

VERSION = "FLOW_WEIGHTING_HOOK_V3_PRIOR_SEPARATION"
VENUE_WEIGHT_PRIORS = {
    "spot": 1.00,
    "futures": 1.00,
    "coinbase": 0.95,
}


def _f(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def install(flow_lead_module):
    if getattr(flow_lead_module, "_flow_weighting_hooked", False):
        return

    def weighted_flow_mean(row):
        venues = row.get("venues") or {}
        items = []
        for name, data in venues.items():
            if not isinstance(data, dict):
                continue
            imbalance = _f(data.get("signed_imbalance"))
            volume = max(0.0, _f(data.get("volume_btc")))
            venue_prior = VENUE_WEIGHT_PRIORS.get(str(name).lower(), 0.90)
            raw_weight = venue_prior * math.sqrt(max(volume, 1e-9))
            items.append((imbalance, raw_weight))

        if not items:
            return 0.0

        raw_weights = [w for _, w in items]
        mean_weight = sum(raw_weights) / len(raw_weights)
        if mean_weight <= 0.0:
            return sum(v for v, _ in items) / len(items)

        weighted_sum = 0.0
        weight_sum = 0.0
        for imbalance, raw_weight in items:
            # Cap venue influence so one high-volume venue cannot dominate the quorum context.
            weight = max(0.55, min(1.80, raw_weight / mean_weight))
            weighted_sum += imbalance * weight
            weight_sum += weight

        return weighted_sum / weight_sum if weight_sum > 0.0 else 0.0

    original_analyze = flow_lead_module.analyze

    def analyze(state, side):
        report = original_analyze(state, side)
        freshness = dict(report.get("freshness") or {})
        report["venue_weight_priors"] = dict(VENUE_WEIGHT_PRIORS)
        report["dynamic_feed_quality"] = {
            "aligned": bool(freshness.get("aligned")),
            "skew_s": freshness.get("skew_s"),
            "used_as_static_weight": False,
        }
        report["weighting_policy"] = (
            "STATIC_VENUE_PRIOR_X_VOLUME_FEED_QUALITY_SEPARATE"
        )
        return report

    flow_lead_module._flow_mean = weighted_flow_mean
    flow_lead_module.analyze = analyze
    flow_lead_module._flow_weighting_hooked = True
