import sys
sys.path.append('../')
from ggl import circuit, logic, io, plexers

# A positive-edge-triggered D flip-flop with enable and asynchronous clear,
# built as a master-slave pair of gated D-latches (see d-latch-clr.py for the
# latch itself). The two latches are clocked on opposite phases:
#   master (dlatchclr_2): transparent while CLK low  (clocked by NOT CLK)
#   slave  (dlatchclr_1): transparent while CLK high (clocked by CLK)
# so a value moves master->slave only on the rising edge. A mux on the master's
# D forms a synchronous enable: EN ? D : Q, so EN=0 recirculates Q (hold).
#
# NOTE: the front-end originally emitted two stray wires that reused legitimate
# wire js_ids and, because a later connect() to a port overwrites the earlier
# one, drove the master's clock (not0) and clear from the slave's Q instead of
# from CLK/CLR. That closed a combinational loop with no fixpoint, so settle()
# never converged. The correct wiring is below.

# --- SR latch subcircuit ---
sr = circuit.Circuit(js_logging=True)
r = io.Input(bits=1, label="r")
r.value = 0
s = io.Input(bits=1, label="s")
s.value = 0
q = io.Output(bits=1, label="q", js_id="output_1")
notq = io.Output(bits=1, label="notq", js_id="output_2")
nor2 = logic.Nor()
nor1 = logic.Nor()
sr.connect(s, nor2.input("1"))       # s    -> nor2.in[1]
sr.connect(r, nor1.input("0"))       # r    -> nor1.in[0]
sr.connect(nor2, nor1.input("1"))    # nor2 -> nor1.in[1]
sr.connect(nor1, nor2.input("0"))    # nor1 -> nor2.in[0]
sr.connect(nor1, q)                  # nor1 -> q
sr.connect(nor2, notq)               # nor2 -> notq

sr_latch = circuit.Component(sr)

# --- gated D-latch with clear (reused for master and slave) ---
dlatchclr = circuit.Circuit()

clk = io.Input(bits=1, label="CLK")
clk.value = 0
d = io.Input(bits=1, label="D")
d.value = 0
clr = io.Input(bits=1, label="CLR")
clr.value = 1
or0 = logic.Or()
and0 = logic.And(inverted_inputs=[1])
and1 = logic.And(inverted_inputs=[1])
and2 = logic.And()
srlatch_1 = sr_latch()
Q = io.Output(bits=1, label="Q", js_id="output_1_1753221650549")
notQ = io.Output(bits=1, label="notQ", js_id="output_2_1753221652561")

dlatchclr.connect(srlatch_1.output("q"), Q)          # srlatch_1 -> Q
dlatchclr.connect(srlatch_1.output("notq"), notQ)    # srlatch_1.notq -> notQ
dlatchclr.connect(clk, or0.input("0"))               # CLK -> or0.in[0]  (enable = CLK OR CLR)
dlatchclr.connect(clr, or0.input("1"))               # CLR -> or0.in[1]
dlatchclr.connect(d, and0.input("0"))                # D   -> and0.in[0] (eff_D = D AND NOT CLR)
dlatchclr.connect(clr, and0.input("1"))              # CLR -> and0.in[1] (inverted)
dlatchclr.connect(or0, and1.input("0"))              # or0  -> and1.in[0]
dlatchclr.connect(and0, and1.input("1"))             # and0 -> and1.in[1] (inverted) => r
dlatchclr.connect(and0, and2.input("1"))             # and0 -> and2.in[1]
dlatchclr.connect(or0, and2.input("0"))              # or0  -> and2.in[0]           => s
dlatchclr.connect(and2, srlatch_1.input("s"))        # and2 -> s
dlatchclr.connect(and1, srlatch_1.input("r"))        # and1 -> r

dlatchclr = circuit.Component(dlatchclr)

# --- master-slave D flip-flop ---
circuit0 = circuit.Circuit(js_logging=True)

input0 = io.Input(label="D", bits=1, js_id="input_1_1753687049207")
input0.value = 0
input1 = io.Input(label="EN", bits=1, js_id="input_2_1753687058028")
input1.value = 1
input2 = io.Input(label="CLR", bits=1, js_id="input_3_1753687058247")
input2.value = 1                     # start cleared for a known initial state
mux0 = plexers.Multiplexer(selector_bits=1, js_id="multiplexer_1_1753687440528")
dlatchclr_1 = dlatchclr()            # slave  (clocked by CLK)
dlatchclr_2 = dlatchclr()            # master (clocked by NOT CLK)
output0 = io.Output(label="Q", bits=1, js_id="output_1_1753687420425")
clk0 = io.Clock(frequency=1, label="CLK", mode="auto", js_id="clock_1_1753687084421")
clk0.value = 0
not0 = logic.Not(num_inputs=1, js_id="not-gate_1_1753687214640")

circuit0.connect(input0, mux0.input("1"))                       # D  -> mux.in[1]
circuit0.connect(input1, mux0.input("sel"))                     # EN -> mux.sel
circuit0.connect(dlatchclr_2.output("Q"), dlatchclr_1.input("D"))  # master Q -> slave D
circuit0.connect(clk0, dlatchclr_1.input("CLK"))                # CLK      -> slave CLK
circuit0.connect(clk0, not0)                                    # CLK      -> NOT
circuit0.connect(not0, dlatchclr_2.input("CLK"))               # NOT CLK  -> master CLK
circuit0.connect(mux0, dlatchclr_2.input("D"))                 # mux      -> master D
circuit0.connect(input2, dlatchclr_1.input("CLR"))            # CLR      -> slave CLR
circuit0.connect(input2, dlatchclr_2.input("CLR"))            # CLR      -> master CLR
circuit0.connect(dlatchclr_1.output("Q"), mux0.input("0"))    # slave Q  -> mux.in[0] (hold path)
circuit0.connect(dlatchclr_1.output("Q"), output0)            # slave Q  -> output

# Settle the initial state: CLR=1 holds Q at 0.
circuit0.run()
print(f"CLR=1 (init):        Q={output0.value}")

# Release the clear; capture happens only on rising CLK edges (cycle()).
input2.value = 0

input0.value = 1
input1.value = 1
circuit0.cycle()
print(f"D=1, EN=1 (capture): Q={output0.value}")

# Enable low: the rising edge ignores D and recirculates Q (hold).
input1.value = 0
input0.value = 0
circuit0.cycle()
print(f"D=0, EN=0 (hold):    Q={output0.value}")

# Enable high again captures the new D on the next edge.
input1.value = 1
input0.value = 0
circuit0.cycle()
print(f"D=0, EN=1 (capture): Q={output0.value}")

input0.value = 1
circuit0.cycle()
print(f"D=1, EN=1 (capture): Q={output0.value}")

# Asynchronous clear forces Q to 0 with no edge.
input2.value = 1
circuit0.settle()
print(f"CLR=1 (async clear): Q={output0.value}")

circuit0.stop()
