import sys
sys.path.append('../')

from ggl import circuit, io, memory

# Edge-triggered register with asynchronous clear. CLK is driven by a plain
# Input toggled by hand (the register detects the 0->1 edge itself), so the
# whole thing settles synchronously without a free-running clock.
c = circuit.Circuit()
d = io.Input(bits=8, label='D')
clk = io.Input(bits=1, label='CLK')
en = io.Input(bits=1, label='EN')
clr = io.Input(bits=1, label='CLR')
reg = memory.RegisterClr(bits=8)
q = io.Output(bits=8, label='Q')

c.connect(d, reg.input('D'))
c.connect(clk, reg.input('CLK'))
c.connect(en, reg.input('en'))
c.connect(clr, reg.input('CLR'))
c.connect(reg.output('Q'), q)

# 1) Asynchronous clear establishes a known 0 from the undefined power-up state,
#    with no clock edge needed.
clk.value = 0
en.value = 1
d.value = 0xAB
clr.value = 1
c.settle()
print(hex(q.value))   # 0x0

# 2) Release clear; with the clock still low the register holds 0...
clr.value = 0
c.settle()
print(hex(q.value))   # 0x0
# ...and loads D on the rising edge.
clk.value = 1
c.settle()
print(hex(q.value))   # 0xab

# 3) Drop the clock and change D: the register holds (edge-triggered, not level).
clk.value = 0
d.value = 0x55
c.settle()
print(hex(q.value))   # 0xab

# 4) Asynchronous clear again, mid-run, forces 0 regardless of the clock.
clr.value = 1
c.settle()
print(hex(q.value))   # 0x0
