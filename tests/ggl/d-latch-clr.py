import sys
sys.path.append('../')
from ggl import circuit, logic, io

# A gated D-latch with asynchronous clear, built from gates exactly as the
# front-end generates it: a NOR SR-latch subcircuit driven by
#   enable  = CLK OR CLR
#   eff_D   = D AND NOT CLR
#   s = enable AND eff_D,   r = enable AND NOT eff_D
# So CLR forces a reset, CLK high makes the latch transparent to D, and CLK low
# holds. A latch cold-starts in an undefined state, so the sequence below does
# what you'd do at the bench: pulse CLR to establish a known 0, release it, then
# drive D/CLK to store and hold a value. Each input assignment + settle() is the
# headless equivalent of the front-end's update_input().

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

# --- D-latch-with-clear ---
d_latch_clr = circuit.Circuit()

clk = io.Clock(frequency=1, label="CLK", mode="auto")
clk.value = 0
d = io.Input(bits=1, label="D")
d.value = 0
clr = io.Input(bits=1, label="CLR")
clr.value = 1                        # start cleared for a known initial state
or0 = logic.Or()
and0 = logic.And(inverted_inputs=[1])
and1 = logic.And(inverted_inputs=[1])
and2 = logic.And()
srlatch_1 = sr_latch()
Q = io.Output(bits=1, label="Q", js_id="output_1_1753221650549")
notQ = io.Output(bits=1, label="notQ", js_id="output_2_1753221652561")

d_latch_clr.connect(srlatch_1.output("q"), Q)          # srlatch_1 -> Q
d_latch_clr.connect(srlatch_1.output("notq"), notQ)    # srlatch_1.notq -> notQ
d_latch_clr.connect(clk, or0.input("0"))               # CLK -> or0.in[0]
d_latch_clr.connect(clr, or0.input("1"))               # CLR -> or0.in[1]
d_latch_clr.connect(d, and0.input("0"))                # D   -> and0.in[0]
d_latch_clr.connect(clr, and0.input("1"))              # CLR -> and0.in[1] (inverted)
d_latch_clr.connect(or0, and1.input("0"))              # or0  -> and1.in[0]
d_latch_clr.connect(and0, and1.input("1"))             # and0 -> and1.in[1] (inverted)
d_latch_clr.connect(and0, and2.input("1"))             # and0 -> and2.in[1]
d_latch_clr.connect(or0, and2.input("0"))              # or0  -> and2.in[0]
d_latch_clr.connect(and2, srlatch_1.input("s"))        # and2 -> s
d_latch_clr.connect(and1, srlatch_1.input("r"))        # and1 -> r

# Settle the initial state: CLR=1 holds Q at 0.
d_latch_clr.run()
print(f"CLR=1 (init):        Q={Q.value}, ~Q={notQ.value}")

# Release the clear; the latch keeps its known 0.
clr.value = 0
d_latch_clr.settle()

# CLK high makes the latch transparent: it stores D=1.
d.value = 1
clk.value = 1
d_latch_clr.settle()
print(f"CLK=1, D=1 (load 1): Q={Q.value}, ~Q={notQ.value}")

# CLK low: the latch holds the stored 1 even as D falls.
clk.value = 0
d.value = 0
d_latch_clr.settle()
print(f"CLK=0, D=0 (hold):   Q={Q.value}, ~Q={notQ.value}")

# CLK high again stores the new D=0.
clk.value = 1
d_latch_clr.settle()
print(f"CLK=1, D=0 (load 0): Q={Q.value}, ~Q={notQ.value}")

# CLK low: the latch holds the stored 0 even as D rises.
clk.value = 0
d.value = 1
d_latch_clr.settle()
print(f"CLK=0, D=1 (hold):   Q={Q.value}, ~Q={notQ.value}")

d_latch_clr.stop()
