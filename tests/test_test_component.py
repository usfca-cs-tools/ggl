"""Tests for io.Test — a truth-table verification directive.

On the first problem (bad column name, malformed row, or a failing row) evaluate()
raises a CircuitError — the same structured-error path the engine uses for open
inputs and bit-width mismatches — and restores the circuit's inputs first. On
success it emits a 'test' pass event and returns.
"""

import pytest

from ggl import callbacks, circuit, io, logic
from ggl.errors import CircuitError


@pytest.fixture(autouse=True)
def clear_callback():
    callbacks.set_callback(None)
    yield
    callbacks.set_callback(None)


def make_and_gate():
    """A 2-input AND gate with Inputs 'A'/'B' and Output 'Y'."""
    c = circuit.Circuit()
    a = io.Input(bits=1, label="A"); a.value = 0
    b = io.Input(bits=1, label="B"); b.value = 0
    g = logic.And()
    c.connect(a, g.input("0"))
    c.connect(b, g.input("1"))
    y = io.Output(bits=1, label="Y", js_id="out_y")
    c.connect(g, y)
    return c


AND_ROWS = [[0, 0, 0], [0, 1, 0], [1, 0, 0], [1, 1, 1]]  # A, B | Y


def test_and_gate_truth_table_passes():
    c = make_and_gate()
    t = io.Test(label="AND", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=AND_ROWS)
    result = t.evaluate(c)
    assert result["passed"] is True


def test_multiple_output_columns_pass():
    c = circuit.Circuit()
    a = io.Input(bits=1, label="A"); a.value = 0
    b = io.Input(bits=1, label="B"); b.value = 0
    andg = logic.And(); org = logic.Or()
    c.connect(a, andg.input("0")); c.connect(b, andg.input("1"))
    c.connect(a, org.input("0")); c.connect(b, org.input("1"))
    c.connect(andg, io.Output(bits=1, label="AND", js_id="o1"))
    c.connect(org, io.Output(bits=1, label="OR", js_id="o2"))
    t = io.Test(label="both", js_id="t1",
                input_names=["A", "B"], output_names=["AND", "OR"],
                rows=[[0, 0, 0, 0], [0, 1, 0, 1], [1, 1, 1, 1]])
    assert t.evaluate(c)["passed"] is True


def test_failing_row_raises_structured_error():
    c = make_and_gate()
    rows = [[0, 0, 0], [0, 1, 0], [1, 0, 0], [1, 1, 0]]  # last row wrong
    t = io.Test(label="AND", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=rows)
    with pytest.raises(CircuitError) as ei:
        t.evaluate(c)
    d = ei.value.to_dict()
    assert ei.value.error_code == "testFailed"
    assert d["component_id"] == "t1"
    assert d["inputs"] == "A=1, B=1"
    assert d["output"] == "Y"
    assert d["expected"] == 0
    assert d["actual"] == 1


def test_missing_input_name_raises():
    c = make_and_gate()
    t = io.Test(label="x", js_id="t1",
                input_names=["A", "D"], output_names=["Y"], rows=[[0, 0, 0]])
    with pytest.raises(CircuitError) as ei:
        t.evaluate(c)
    assert ei.value.error_code == "testInputNotFound"
    assert ei.value.to_dict()["name"] == "D"


def test_missing_output_name_raises():
    c = make_and_gate()
    t = io.Test(label="x", js_id="t1",
                input_names=["A", "B"], output_names=["Z"], rows=[[0, 0, 0]])
    with pytest.raises(CircuitError) as ei:
        t.evaluate(c)
    assert ei.value.error_code == "testOutputNotFound"
    assert ei.value.to_dict()["name"] == "Z"


def test_ambiguous_name_raises():
    c = circuit.Circuit()
    a1 = io.Input(bits=1, label="A"); a1.value = 0
    a2 = io.Input(bits=1, label="A"); a2.value = 0   # duplicate label
    g = logic.Or()
    c.connect(a1, g.input("0")); c.connect(a2, g.input("1"))
    c.connect(g, io.Output(bits=1, label="Y", js_id="out_y"))
    t = io.Test(label="x", js_id="t1",
                input_names=["A"], output_names=["Y"], rows=[[1, 1]])
    with pytest.raises(CircuitError) as ei:
        t.evaluate(c)
    assert ei.value.error_code == "testAmbiguousLabel"


def test_row_width_mismatch_raises():
    c = make_and_gate()
    t = io.Test(label="x", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=[[0, 0]])
    with pytest.raises(CircuitError) as ei:
        t.evaluate(c)
    d = ei.value.to_dict()
    assert ei.value.error_code == "testRowWidth"
    assert (d["row"], d["actual"], d["expected"]) == (1, 2, 3)


def test_restores_inputs_after_pass():
    c = make_and_gate()  # A, B authored to 0
    t = io.Test(label="AND", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=AND_ROWS)
    t.evaluate(c)
    a = next(i for i in c.inputs if i.label == "A")
    b = next(i for i in c.inputs if i.label == "B")
    y = next(o for o in c.outputs if o.label == "Y")
    assert (a.value, b.value) == (0, 0)
    assert y.value == 0


def test_restores_inputs_after_failure():
    c = make_and_gate()
    t = io.Test(label="AND", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=[[1, 1, 0]])
    with pytest.raises(CircuitError):
        t.evaluate(c)  # AND(1,1)=1 != 0
    a = next(i for i in c.inputs if i.label == "A")
    y = next(o for o in c.outputs if o.label == "Y")
    assert a.value == 0   # restored despite the failure
    assert y.value == 0


def test_emits_pass_badge():
    events = []
    callbacks.set_callback(lambda ev, cid, p: events.append((ev, cid, p)))
    c = make_and_gate()
    t = io.Test(label="AND", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=AND_ROWS)
    t.evaluate(c)
    test_events = [e for e in events if e[0] == "test"]
    assert test_events and test_events[-1][1] == "t1"
    assert test_events[-1][2]["passed"] is True


def test_emits_fail_badge_before_raising():
    events = []
    callbacks.set_callback(lambda ev, cid, p: events.append((ev, cid, p)))
    c = make_and_gate()
    t = io.Test(label="AND", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=[[1, 1, 0]])
    with pytest.raises(CircuitError):
        t.evaluate(c)
    test_events = [e for e in events if e[0] == "test"]
    assert test_events and test_events[-1][2]["passed"] is False
