import sys
sys.path.append('../')

from ggl import circuit, io, memory

c = circuit.Circuit()

d = io.Input(bits=1, label='D')
clk = io.Input(bits=1, label='CLK')   # ordinary signal, toggled by hand
en = io.Input(bits=1, label='EN')
r = memory.Register(bits=1)
q = io.Output(bits=1, label='Q')

# Connect the components
c.connect(d, r.input('D'))
c.connect(clk, r.input('CLK'))
c.connect(en, r.input('en'))
c.connect(r.output('Q'), q)

# Set inputs, then pulse the clock 0 -> 1 to load D.
d.value = 1
en.value = 1
clk.value = 0
c.settle()
clk.value = 1
c.settle()

# Check output
print(q.value)
