"""Tests for io.Test — a truth-table verification directive.

On the first problem (bad column name, malformed row, or a failing row) evaluate()
raises a CircuitError — the same structured-error path the engine uses for open
inputs and bit-width mismatches — and restores the circuit's inputs first. On
success it emits a 'test' pass event and returns.
"""

import asyncio

import pytest

from ggl import arithmetic, callbacks, circuit, io, logic, memory, plexers
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


# --- Clocked "stop when output reaches a value" mode ------------------------

def make_counter():
    """8-bit up-counter driven by a Clock, cleared to 0. Outputs:
    'count' (the counter) and 'flag' (a constant 42, to sample at the stop)."""
    c = circuit.Circuit()
    clk = io.Clock(label="CLK")
    clr = io.Input(label="CLR", bits=1); clr.value = 0
    reg = memory.RegisterClr(label="REG", bits=8)
    adder = arithmetic.Adder(label="+", bits=8)
    en = io.Constant(bits=1); en.value = 1
    inc = io.Constant(bits=8); inc.value = 1
    cin = io.Constant(bits=1); cin.value = 0
    c.connect(clk, reg.input("CLK"))
    c.connect(clr, reg.input("CLR"))
    c.connect(en, reg.input("en"))
    c.connect(inc, adder.input("b"))
    c.connect(cin, adder.input("cin"))
    c.connect(adder.output("sum"), reg.input("D"))
    c.connect(reg.output("Q"), io.Output(label="count", bits=8, js_id="count"))
    c.connect(reg.output("Q"), adder.input("a"))
    flag_const = io.Constant(bits=8); flag_const.value = 42
    c.connect(flag_const, io.Output(label="flag", bits=8, js_id="flag"))
    # Clear to a known 0 (register has no defined power-up state).
    clr.value = 1; c.run(); clr.value = 0; c.settle()
    return c


def test_clocked_runs_until_stop_then_checks_pass():
    c = make_counter()
    # Pulse the clock until 'count' reaches 5, then sample 'flag' (== 42).
    t = io.Test(label="run", js_id="t1",
                input_names=[], output_names=["flag"], rows=[[42]],
                stop_enabled=True, stop_output_name="count", stop_output_value=5)
    assert t.evaluate(c)["passed"] is True


def test_clocked_wrong_sampled_output_fails():
    c = make_counter()
    t = io.Test(label="run", js_id="t1",
                input_names=[], output_names=["flag"], rows=[[41]],  # flag is 42
                stop_enabled=True, stop_output_name="count", stop_output_value=5)
    with pytest.raises(CircuitError) as ei:
        t.evaluate(c)
    d = ei.value.to_dict()
    assert ei.value.error_code == "testFailed"
    assert (d["output"], d["expected"], d["actual"]) == ("flag", 41, 42)
    assert "count=5" in d["inputs"]   # the stop condition is described


def test_clocked_stop_never_reached_fails():
    c = make_counter()
    t = io.Test(label="run", js_id="t1",
                input_names=[], output_names=["flag"], rows=[[42]],
                stop_enabled=True, stop_output_name="count", stop_output_value=200,
                max_cycles=10)   # count only reaches 10 in 10 cycles
    with pytest.raises(CircuitError) as ei:
        t.evaluate(c)
    d = ei.value.to_dict()
    assert ei.value.error_code == "testStopNotReached"
    assert (d["name"], d["value"], d["cycles"]) == ("count", 200, 10)


def test_clocked_without_a_clock_fails():
    c = make_and_gate()  # combinational, no clock
    t = io.Test(label="x", js_id="t1",
                input_names=["A", "B"], output_names=["Y"], rows=[[0, 0, 0]],
                stop_enabled=True, stop_output_name="Y", stop_output_value=1)
    with pytest.raises(CircuitError) as ei:
        t.evaluate(c)
    assert ei.value.error_code == "testNoClock"


# --- Reset pulse (initialize a sequential circuit before running) -----------

def make_loadable_counter():
    """Counter that loads input 'B' into its register when 'CLR' is pulsed high,
    else counts up — mirrors the user's instruction-counter (mux-select load).
    A plain Register powers up at 0, so a wrong/absent reset is detectable."""
    c = circuit.Circuit()
    clk = io.Clock(label="CLK")
    clr = io.Input(label="CLR", bits=1); clr.value = 0
    b = io.Input(label="B", bits=8); b.value = 0
    reg = memory.Register(label="REG", bits=8)
    adder = arithmetic.Adder(label="+", bits=8)
    mux = plexers.Multiplexer(selector_bits=1, bits=8)
    en = io.Constant(bits=1); en.value = 1
    inc = io.Constant(bits=8); inc.value = 1
    cin = io.Constant(bits=1); cin.value = 0
    c.connect(clk, reg.input("CLK"))
    c.connect(en, reg.input("en"))
    c.connect(inc, adder.input("a"))
    c.connect(cin, adder.input("cin"))
    c.connect(reg.output("Q"), adder.input("b"))       # sum = Q + 1
    c.connect(clr, mux.input("sel"))
    c.connect(adder.output("sum"), mux.input("0"))     # CLR=0 -> load Q+1 (count)
    c.connect(b, mux.input("1"))                       # CLR=1 -> load B (reset)
    c.connect(mux, reg.input("D"))
    c.connect(reg.output("Q"), io.Output(label="count", bits=8, js_id="count"))
    return c


def test_reset_pulse_loads_known_state():
    # Pulsing CLR high for one cycle must load B into the register (== 7 here);
    # without the reset the register would still read its power-up 0.
    c = make_loadable_counter()
    t = io.Test(label="reset", js_id="t1",
                input_names=["B"], output_names=["count"], rows=[[7, 7]],
                reset_enabled=True, reset_input_name="CLR")
    assert t.evaluate(c)["passed"] is True


def test_reset_then_run_until_stop():
    c = make_loadable_counter()
    # Load B=100, then count until 'count' reaches 103; expect 103.
    t = io.Test(label="run", js_id="t1",
                input_names=["B"], output_names=["count"], rows=[[100, 103]],
                reset_enabled=True, reset_input_name="CLR",
                stop_enabled=True, stop_output_name="count", stop_output_value=103)
    assert t.evaluate(c)["passed"] is True


def test_clocked_run_with_no_rows_still_runs():
    # No columns and no rows: just reset + run until 'count' reaches 5. The
    # clocked run must still execute (one implicit scenario), not no-op.
    c = make_loadable_counter()
    t = io.Test(label="run", js_id="t1",
                input_names=[], output_names=[], rows=[],
                reset_enabled=True, reset_input_name="CLR",
                stop_enabled=True, stop_output_name="count", stop_output_value=5)
    # Reset loads B (0), then count 0->5; passes with nothing to assert.
    assert t.evaluate(c)["passed"] is True


def test_reset_input_not_found():
    c = make_loadable_counter()
    t = io.Test(label="x", js_id="t1",
                input_names=["B"], output_names=["count"], rows=[[0, 0]],
                reset_enabled=True, reset_input_name="NOPE")
    with pytest.raises(CircuitError) as ei:
        t.evaluate(c)
    assert ei.value.error_code == "testInputNotFound"
    assert ei.value.to_dict()["name"] == "NOPE"


# --- Cooperative (browser) path: evaluate_async ---------------------------------
# evaluate() (headless) and evaluate_async() (browser) share one generator, so they
# must agree; evaluate_async additionally yields between clock cycles and honors a
# mid-run Stop (circuit.stop_requested), restoring the circuit like a normal finish.

def _clocked_test():
    return io.Test(label="run", js_id="t1",
                   input_names=["B"], output_names=["count"], rows=[[100, 103]],
                   reset_enabled=True, reset_input_name="CLR",
                   stop_enabled=True, stop_output_name="count", stop_output_value=103)


def test_evaluate_async_matches_evaluate_for_clocked_test():
    sync_result = _clocked_test().evaluate(make_loadable_counter())
    async_result = asyncio.run(_clocked_test().evaluate_async(make_loadable_counter()))
    assert async_result["passed"] is True
    assert async_result == sync_result


def test_evaluate_async_returns_cancelled_when_already_stopped():
    c = make_loadable_counter()
    c.stop()  # a Stop that arrived before this Test's turn
    result = asyncio.run(_clocked_test().evaluate_async(c))
    assert result["cancelled"] is True
    assert result["passed"] is None


def test_evaluate_async_stop_mid_run_cancels_and_restores(monkeypatch):
    # Yield every cycle so a concurrent Stop interleaves deterministically.
    monkeypatch.setattr(io, "_TEST_YIELD_INTERVAL_S", 0)
    c = make_loadable_counter()
    b = next(n for n in c.inputs if n.label == "B")
    b.value = 7  # authored input, to prove restore() returns to it after abort
    # A stop value the 8-bit counter never reaches, so without the Stop it would
    # run to the max_cycles cap.
    t = io.Test(label="run", js_id="t1",
                input_names=["B"], output_names=["count"], rows=[[100, 999]],
                reset_enabled=True, reset_input_name="CLR",
                stop_enabled=True, stop_output_name="count", stop_output_value=999)

    async def scenario():
        task = asyncio.create_task(t.evaluate_async(c))
        for _ in range(3):       # let it pulse a few cycles...
            await asyncio.sleep(0)
        c.stop()                 # ...then request Stop mid-run
        return await task

    result = asyncio.run(scenario())
    assert result["cancelled"] is True
    assert b.value == 7  # restored to the authored value, not the row's 100
