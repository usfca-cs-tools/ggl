"""Tests for io.Test — a truth-table verification directive. It names Input and
Output columns (by component label) and holds one row per vector; evaluate()
drives the inputs, settles, and checks the outputs for every row.

evaluate() is the single source of truth for pass/fail: it returns a structured
result and (when a host callback is registered) emits a 'test' event with it.
"""

import pytest

from ggl import callbacks, circuit, io, logic


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
    assert result["failures"] == []
    assert result["errors"] == []


def test_failing_row_names_the_inputs_and_columns():
    c = make_and_gate()
    # Same table but the last row expects Y=0 for A=1,B=1 (should be 1).
    rows = [[0, 0, 0], [0, 1, 0], [1, 0, 0], [1, 1, 0]]
    t = io.Test(label="AND", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=rows)
    result = t.evaluate(c)
    assert result["passed"] is False
    assert result["failures"] == ["For A=1, B=1: expected Y=0, got 1"]


def test_restores_inputs_after_run():
    # After the table runs, the circuit must reflect its authored inputs (A=0,
    # B=0 -> Y=0), not the last row it drove (A=1, B=1 -> Y=1).
    c = make_and_gate()  # A and B authored to 0
    t = io.Test(label="AND", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=AND_ROWS)
    t.evaluate(c)
    a = next(i for i in c.inputs if i.label == "A")
    b = next(i for i in c.inputs if i.label == "B")
    y = next(o for o in c.outputs if o.label == "Y")
    assert (a.value, b.value) == (0, 0)
    assert y.value == 0


def test_multiple_output_columns():
    # Two outputs Sum/Cout from a half adder-ish check on the AND (Cout only).
    c = make_and_gate()
    # Add a second output that mirrors A via an OR with 0 is overkill; instead
    # just verify multi-column mechanics by naming Y twice is ambiguous — so use
    # a fresh circuit with two outputs.
    c2 = circuit.Circuit()
    a = io.Input(bits=1, label="A"); a.value = 0
    b = io.Input(bits=1, label="B"); b.value = 0
    andg = logic.And(); org = logic.Or()
    c2.connect(a, andg.input("0")); c2.connect(b, andg.input("1"))
    c2.connect(a, org.input("0")); c2.connect(b, org.input("1"))
    c2.connect(andg, io.Output(bits=1, label="AND", js_id="o1"))
    c2.connect(org, io.Output(bits=1, label="OR", js_id="o2"))
    t = io.Test(label="both", js_id="t1",
                input_names=["A", "B"], output_names=["AND", "OR"],
                rows=[[0, 0, 0, 0], [0, 1, 0, 1], [1, 1, 1, 1]])  # A,B | AND,OR
    assert t.evaluate(c2)["passed"] is True


def test_missing_input_name_is_an_error():
    c = make_and_gate()
    t = io.Test(label="badin", js_id="t1",
                input_names=["A", "D"], output_names=["Y"], rows=[[0, 0, 0]])
    result = t.evaluate(c)
    assert result["passed"] is False
    assert "Input 'D' not found" in result["errors"]
    assert result["failures"] == []  # bad column short-circuits before rows run


def test_missing_output_name_is_an_error():
    c = make_and_gate()
    t = io.Test(label="badout", js_id="t1",
                input_names=["A", "B"], output_names=["Z"], rows=[[0, 0, 0]])
    result = t.evaluate(c)
    assert result["passed"] is False
    assert "Output 'Z' not found" in result["errors"]


def test_ambiguous_name_is_an_error():
    c = circuit.Circuit()
    a1 = io.Input(bits=1, label="A"); a1.value = 0
    a2 = io.Input(bits=1, label="A"); a2.value = 0   # duplicate label
    g = logic.Or()
    c.connect(a1, g.input("0")); c.connect(a2, g.input("1"))
    c.connect(g, io.Output(bits=1, label="Y", js_id="out_y"))
    t = io.Test(label="amb", js_id="t1",
                input_names=["A"], output_names=["Y"], rows=[[1, 1]])
    result = t.evaluate(c)
    assert result["passed"] is False
    assert any("ambiguous" in e for e in result["errors"])


def test_row_width_mismatch_is_an_error():
    c = make_and_gate()
    t = io.Test(label="bad", js_id="t1",
                input_names=["A", "B"], output_names=["Y"],
                rows=[[0, 0]])  # needs 3 values (A, B, Y)
    result = t.evaluate(c)
    assert result["passed"] is False
    assert "Row 1 has 2 values, expected 3" in result["errors"]


def test_emits_test_event_to_registered_callback():
    events = []
    callbacks.set_callback(lambda ev, cid, payload: events.append((ev, cid, payload)))
    c = make_and_gate()
    t = io.Test(label="AND", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=AND_ROWS)
    result = t.evaluate(c)
    test_events = [e for e in events if e[0] == "test"]
    assert len(test_events) == 1
    ev, cid, payload = test_events[0]
    assert cid == "t1"
    assert payload["passed"] is True
    assert payload is result
