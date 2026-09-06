import pytest
from loi_he_thong import causal_mechanism

def test_causal_mechanism_position_build():
    oi = {"status": "FRESH_POSITION_BUILD", "intent": "POSITION_BUILD"}
    assert causal_mechanism.classify(oi, {}, {}) == "POSITION_BUILD"

def test_causal_mechanism_unwind_no_liquidation():
    oi = {"status": "FRESH_UNWIND", "intent": "UNWIND"}
    assert causal_mechanism.classify(oi, {}, {}) == "UNWIND"

def test_causal_mechanism_forced_closing():
    oi = {"status": "FRESH_UNWIND", "intent": "UNWIND"}
    liq = {"burst": True, "decelerating": False}
    assert causal_mechanism.classify(oi, liq, {"dual_cash_independent": True}) == "FORCED_CLOSING"
    assert causal_mechanism.classify(oi, liq, {}) == "FORCED_CLOSING"

def test_causal_mechanism_cash_control_after_unwind():
    oi = {"status": "FRESH_UNWIND", "intent": "UNWIND"}
    liq = {"burst": True, "decelerating": True}
    assert causal_mechanism.classify(oi, liq, {"dual_cash_independent": True}) == "CASH_CONTROL_AFTER_UNWIND"

def test_causal_mechanism_forced_closing_decelerating_no_cash():
    oi = {"status": "FRESH_UNWIND", "intent": "UNWIND"}
    liq = {"burst": True, "decelerating": True}
    assert causal_mechanism.classify(oi, liq, {"dual_cash_independent": False}) == "FORCED_CLOSING"

def test_causal_mechanism_unresolved():
    liq = {"burst": False, "decelerating": False}
    assert causal_mechanism.classify({"status": "STALE_UNKNOWN"}, liq, {}) == "UNRESOLVED"
    assert causal_mechanism.classify({"status": "UNCHANGED_UNKNOWN"}, liq, {}) == "UNRESOLVED"
    assert causal_mechanism.classify({"status": "UNAVAILABLE"}, liq, {}) == "UNRESOLVED"
    assert causal_mechanism.classify({"status": "FRESH_CONFLICT"}, liq, {}) == "UNRESOLVED"
