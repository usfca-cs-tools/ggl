"""Synchronous settle must reach the true combinational fixpoint, and the barrel shifter
must accept a shift amount narrower than its data width. Both were surfaced by an ALU whose
`1 << 3` produced 1 and whose mux output stuck at 0."""

from ggl import circuit, io, logic, arithmetic


def test_settle_reaches_fixpoint_through_a_stale_combinational_node():
    # A combinational node writes only to its output edges, never to self.value, so a pass
    # that corrects such a node — but not yet any state-holding node — must still count as a
    # change or settle() stops early. Here A feeds the AND directly (connected FIRST, so the
    # AND fires before the two-NOT buffer delivers its other input): the AND fires once with a
    # stale 0 and outputs 0 (== the Output's initial value). Only edge-change detection keeps
    # iterating to the real fixpoint, A AND A = 1.
    c = circuit.Circuit()
    a = io.Input(bits=1, label="A")
    a.value = 1
    n1 = logic.Not(bits=1)
    n2 = logic.Not(bits=1)  # two inverters = a delayed buffer of A
    g = logic.And(num_inputs=2, bits=1)
    out = io.Output(bits=1, label="R")

    c.connect(a, g.input("0"))  # direct path, wired first -> AND enqueued before the buffer
    c.connect(a, n1.input("0"))
    c.connect(n1, n2.input("0"))
    c.connect(n2, g.input("1"))
    c.connect(g, out.input("0"))

    c.run()
    assert out.value == 1


def test_barrel_shifter_accepts_a_narrow_shift_amount():
    # The shift amount is just a count; it need not match the data width. A 64-bit shifter fed
    # a 6-bit amount (e.g. the low bits split off a wide operand) must shift, not raise a
    # bit-width mismatch or read the amount as 0.
    c = circuit.Circuit()
    data = io.Input(bits=64, label="in")
    data.value = 1
    amount = io.Input(bits=6, label="amt")  # narrower than the 64-bit data
    amount.value = 3
    sh = arithmetic.BarrelShifter(bits=64, direction="left", mode="logical")
    out = io.Output(bits=64, label="R")

    c.connect(data, sh.input("in"))
    c.connect(amount, sh.input("shift"))
    c.connect(sh.output("out"), out.input("0"))

    c.run()
    assert out.value == 8  # 1 << 3
