"""Manual clocking: a Manual-mode clock is advanced one edge at a time by Circuit.tick()
(two ticks = a full low->high->low cycle), while an Auto clock ignores tick() and is driven
only by the free-running loop."""

from ggl import callbacks, circuit, io, arithmetic, memory


def _counter(mode):
    """clock -> RegisterClr(+1 feedback); returns (circuit, clr_input, q_output)."""
    c = circuit.Circuit()
    clk = io.Clock(mode=mode)
    c.clock = clk
    clr = io.Input(bits=1, label="CLR")
    en = io.Input(bits=1, label="EN")
    en.value = 1
    one = io.Constant(bits=8, label="1")
    one.value = 1
    k0 = io.Constant(bits=1, label="0")
    k0.value = 0
    reg = memory.RegisterClr(bits=8, label="PC")
    add = arithmetic.Adder(bits=8)
    q = io.Output(bits=8, label="Q", js_id="q")  # js_id so it emits a UI callback

    c.connect(reg.output("Q"), add.input("a"))
    c.connect(one, add.input("b"))
    c.connect(k0, add.input("cin"))
    c.connect(add.output("sum"), reg.input("D"))
    c.connect(reg.output("Q"), q.input("0"))
    c.connect(clk, reg.input("CLK"))
    c.connect(clr, reg.input("CLR"))
    c.connect(en, reg.input("en"))
    return c, clr, q


def test_manual_clock_advances_one_edge_per_tick():
    c, clr, q = _counter("manual")
    clr.value = 1
    c.settle()                       # async clear -> 0
    assert q.value == 0
    clr.value = 0
    c.settle()

    # Two ticks (rising then falling) = one full cycle = one increment.
    c.tick()                         # 0 -> 1 rising: latch D=1
    assert q.value == 1
    c.tick()                         # 1 -> 0 falling: hold
    assert q.value == 1
    c.tick()                         # 0 -> 1 rising: latch D=2
    assert q.value == 2
    c.tick()                         # falling: hold
    assert q.value == 2


def test_auto_clock_ignores_tick():
    # tick() is for Manual clocks only; an Auto clock is driven by run_async, so tick()
    # must not move it (otherwise a hand-step would race the free-run loop).
    c, clr, q = _counter("auto")
    clr.value = 1
    c.settle()
    clr.value = 0
    c.settle()
    before = q.value
    c.tick()
    c.tick()
    assert q.value == before  # no-op


def test_tick_with_no_clock_is_a_noop():
    c = circuit.Circuit()  # no clock connected
    c.tick()  # must not raise


def test_one_tick_emits_one_ui_batch():
    c, clr, q = _counter("manual")
    clr.value = 0
    c.settle()
    batches = []
    callbacks.set_callback(lambda event, cid, payload: batches.append(event) if event == "batch" else None)
    try:
        c.tick()
    finally:
        callbacks.set_callback(None)
    assert batches == ["batch"]  # exactly one coalesced batch per tick
