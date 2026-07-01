from ggl import arithmetic, circuit, io, memory

# Build the circuit
circuit0 = circuit.Circuit()
clk = io.Input(label="CLK", bits=1)   # ordinary signal, toggled by hand
clr = io.Input(label="CLR", bits=1)
reg = memory.RegisterClr(label="REG", bits=8)
adder = arithmetic.Adder(label="+", bits=8)
out = io.Output(label="count", bits=8)
const_en = io.Constant(bits=1); const_en.value = 1
const_inc = io.Constant(bits=8); const_inc.value = 1
const_cin = io.Constant(bits=1); const_cin.value = 0

# Connect components
circuit0.connect(clk, reg.input("CLK"))
circuit0.connect(clr, reg.input("CLR"))
circuit0.connect(const_en, reg.input("en"))
circuit0.connect(const_inc, adder.input("b"))
circuit0.connect(const_cin, adder.input("cin"))
circuit0.connect(adder.output("sum"), reg.input("D"))
circuit0.connect(reg.output("Q"), out)
circuit0.connect(reg.output("Q"), adder.input("a"))

# Clear to a known 0 (the register has no defined power-up state).
clk.value = 0
clr.value = 1
circuit0.settle()
clr.value = 0

# Count 12 cycles: read the current count, then pulse the clock (0 -> 1 -> 0).
for i in range(12):
    assert out.value == i
    clk.value = 1
    circuit0.settle()
    clk.value = 0
    circuit0.settle()
