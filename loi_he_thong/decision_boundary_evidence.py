"""Recorder-only distance-to-boundary evidence for Phase-4 ablation.

Positive distance means the observed value is on the passing side.  Missing
or semantically unknown evidence remains ``observed=False``; it is never
coerced to zero and therefore cannot look like a near miss.
"""

from loi_he_thong import causal_threshold_registry as registry


VERSION = "DECISION_BOUNDARY_EVIDENCE_V1"


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _row(name, observed, distance, *, source, relation):
    spec = registry.THRESHOLDS[name]
    return {
        "threshold_id": name,
        "category": spec["category"],
        "owner": spec["owner"],
        "question": spec["question"],
        "threshold": spec["value"],
        "unit": spec["unit"],
        "observed": observed is not None,
        "observed_value": observed,
        "distance_to_boundary": distance,
        "passing_relation": relation,
        "source": source,
        "authority": False,
    }


def build(result, edge_report=None):
    result = dict(result or {})
    edge = dict(edge_report or {})
    ignition = dict(result.get("ignition") or {})
    thesis = dict(edge.get("entry_thesis_audit") or {})
    questions = dict(thesis.get("questions") or {})
    q1 = dict(questions.get("q1_bias") or {})
    q3 = dict(questions.get("q3_flow_efficiency") or {})
    q5 = dict(questions.get("q5_maturity") or {})

    bias = _f(q1.get("confidence"), _f(result.get("bias_confidence")))
    flow = _f(q3.get("cash_flow_strength"))
    progress = _f(q3.get("recent_cash_progress_bps"))
    material = _f(q3.get("material_price_bps"))
    consumed = _f(q5.get("shared_wave_consumed"), _f(
        ignition.get("consumed_fraction")
    ))
    response_ms = _f(
        ignition.get("futures_follow_latency_ms"),
        _f(ignition.get("futures_response_ms")),
    )
    basis = dict(edge.get("spot_perp_basis") or {})
    perp_lead = _f(basis.get("lead_bps"))
    expected_net = _f(edge.get("expected_net_bps_model"))
    reserve = _f(edge.get("min_net_edge_bps"))

    rows = {
        "bias_support": _row(
            "bias.minimum_support", bias,
            None if bias is None else round(
                bias - float(registry.value("bias.minimum_support")), 6
            ), source="frozen_bias", relation="observed >= threshold",
        ),
        "flow_imbalance": _row(
            "entry.minimum_flow_imbalance", flow,
            None if flow is None else round(
                flow - float(registry.value("entry.minimum_flow_imbalance")), 6
            ), source="entry_thesis.q3", relation="observed >= threshold",
        ),
        "cash_progress": _row(
            "entry.minimum_cash_progress", progress,
            None if progress is None or material is None else round(
                progress - material, 6
            ), source="entry_thesis.q3", relation="observed >= adaptive threshold",
        ),
        "wave_consumed": _row(
            "entry.max_consumed_fraction", consumed,
            None if consumed is None else round(
                float(registry.value("entry.max_consumed_fraction")) - consumed, 6
            ), source="entry_thesis.q5", relation="observed <= threshold",
        ),
        "futures_follow_age": _row(
            "ignition.follow_window", response_ms,
            None if response_ms is None else round(
                float(registry.value("ignition.follow_window")) - response_ms, 6
            ), source="ignition.futures_follow_latency_ms", relation="observed <= threshold",
        ),
        "perp_lead": _row(
            "entry.perp_lead_veto", perp_lead,
            None if perp_lead is None else round(
                float(registry.value("entry.perp_lead_veto")) - perp_lead, 6
            ), source="entry_edge.spot_perp_basis", relation="observed <= threshold",
        ),
        "economic_reserve": {
            "threshold_id": "economic.minimum_net_reserve",
            "category": "EMPIRICAL_ALPHA",
            "owner": "entry_economics_v2",
            "question": "Does Guardian-net edge clear frozen executable cost?",
            "threshold": reserve,
            "unit": "bps",
            "observed": expected_net is not None and reserve is not None,
            "observed_value": expected_net,
            "distance_to_boundary": (
                round(expected_net - reserve, 6)
                if expected_net is not None and reserve is not None else None
            ),
            "passing_relation": "observed >= threshold",
            "source": "entry_edge.frozen_economics",
            "authority": False,
        },
    }
    return {
        "version": VERSION,
        "authority": False,
        "policy": "OBSERVE_DISTANCE_NEVER_CHANGE_GO_WAIT",
        "boundaries": rows,
    }
