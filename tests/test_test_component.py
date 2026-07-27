"""Tests for io.Test — the verification directive that drives named Inputs,
settles the circuit, and checks named Outputs against expected values.

evaluate() is the single source of truth for pass/fail: it returns a structured
result and (when a host callback is registered) emits a 'test' event carrying
the same result.
"""

import pytest

from ggl import callbacks, circuit, io, logic


@pytest.fixture(autouse=True)
def clear_callback():
    callbacks.set_callback(None)
    yield
    callbacks.set_callback(None)


def make_sr_latch():
    """A NOR SR latch with Inputs 'S'/'R' and Outputs 'Q'/'NotQ' (mirrors
    tests/ggl/sr-latch.py). Inputs start at 0; a Test drives them."""
    c = circuit.Circuit()
    r = io.Input(bits=1, label="R"); r.value = 0
    s = io.Input(bits=1, label="S"); s.value = 0
    nor2 = logic.Nor()
    c.connect(s, nor2.input("1"))
    nor1 = logic.Nor()
    c.connect(r, nor1.input("0"))
    c.connect(nor2, nor1.input("1"))
    c.connect(nor1, nor2.input("0"))
    q = io.Output(bits=1, label="Q", js_id="out_q")
    c.connect(nor1, q)
    notq = io.Output(bits=1, label="NotQ", js_id="out_notq")
    c.connect(nor2, notq)
    return c


def test_passing_vector():
    c = make_sr_latch()
    t = io.Test(label="set", js_id="t1",
                input_specs={"S": 1, "R": 0},
                output_specs={"Q": 1, "NotQ": 0})
    result = t.evaluate(c)
    assert result["passed"] is True
    assert result["failures"] == []
    assert result["errors"] == []


def test_output_mismatch_is_reported():
    c = make_sr_latch()
    t = io.Test(label="wrong", js_id="t1",
                input_specs={"S": 1, "R": 0},
                output_specs={"Q": 0, "NotQ": 0})  # Q expectation is wrong
    result = t.evaluate(c)
    assert result["passed"] is False
    assert result["failures"] == ["Output Q was 1, expected 0"]
    assert result["errors"] == []


def test_missing_input_name_is_an_error():
    c = make_sr_latch()
    t = io.Test(label="badin", js_id="t1",
                input_specs={"D": 1},           # no such input
                output_specs={"Q": 1})
    result = t.evaluate(c)
    assert result["passed"] is False
    assert "Input 'D' not found" in result["errors"]
    # A bad input name short-circuits before output checks run.
    assert result["failures"] == []


def test_missing_output_name_is_an_error():
    c = make_sr_latch()
    t = io.Test(label="badout", js_id="t1",
                input_specs={"S": 1, "R": 0},
                output_specs={"Z": 1})          # no such output
    result = t.evaluate(c)
    assert result["passed"] is False
    assert "Output 'Z' not found" in result["errors"]


def test_ambiguous_name_is_an_error():
    # Two inputs share the label 'A' — resolving 'A' must be flagged, not guessed.
    c = circuit.Circuit()
    a1 = io.Input(bits=1, label="A"); a1.value = 0
    a2 = io.Input(bits=1, label="A"); a2.value = 0
    g = logic.Or()
    c.connect(a1, g.input("0"))
    c.connect(a2, g.input("1"))
    out = io.Output(bits=1, label="Y", js_id="out_y")
    c.connect(g, out)
    t = io.Test(label="amb", js_id="t1",
                input_specs={"A": 1}, output_specs={"Y": 1})
    result = t.evaluate(c)
    assert result["passed"] is False
    assert any("ambiguous" in e for e in result["errors"])


def test_sequential_vectors_on_one_circuit():
    # Each vector re-drives its own inputs and settles independently.
    c = make_sr_latch()
    set_q = io.Test(label="set", js_id="t1",
                    input_specs={"S": 1, "R": 0},
                    output_specs={"Q": 1, "NotQ": 0})
    reset_q = io.Test(label="reset", js_id="t2",
                      input_specs={"S": 0, "R": 1},
                      output_specs={"Q": 0, "NotQ": 1})
    assert set_q.evaluate(c)["passed"] is True
    assert reset_q.evaluate(c)["passed"] is True


def test_emits_test_event_to_registered_callback():
    events = []
    callbacks.set_callback(lambda ev, cid, payload: events.append((ev, cid, payload)))
    c = make_sr_latch()
    t = io.Test(label="set", js_id="t1",
                input_specs={"S": 1, "R": 0},
                output_specs={"Q": 1, "NotQ": 0})
    result = t.evaluate(c)
    test_events = [e for e in events if e[0] == "test"]
    assert len(test_events) == 1
    ev, cid, payload = test_events[0]
    assert cid == "t1"
    assert payload["passed"] is True
    assert payload is result  # same dict that evaluate() returned
