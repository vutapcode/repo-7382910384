"""Volume- and reliability-weighted flow mean for Tier-S persistence analysis."""
import math

VERSION = "FLOW_WEIGHTING_HOOK_V2_VOLUME_BTC"
RELIABILITY = {
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
            reliability = RELIABILITY.get(str(name).lower(), 0.90)
            raw_weight = reliability * math.sqrt(max(volume, 1e-9))
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

    flow_lead_module._flow_mean = weighted_flow_mean
    flow_lead_module._flow_weighting_hooked = True
