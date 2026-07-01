from ggl import io, circuit, plexers

circuit0 = circuit.Circuit()
decoder0 = plexers.Decoder(selector_bits=2, label='decoder0')

sel = io.Input(bits=2, label='SEL')
sel.value = 2
circuit0.connect(sel, decoder0.input("sel"))

r0 = io.Output(bits=1, label='r0')
circuit0.connect(decoder0.output("0"), r0)
r1 = io.Output(bits=1, label='r1')
circuit0.connect(decoder0.output("1"), r1)

r2 = io.Output(bits=1, label='r2')
circuit0.connect(decoder0.output("2"), r2)
r3 = io.Output(bits=1, label='r3')
circuit0.connect(decoder0.output("3"), r3)

circuit0.run()
assert r0.value == 0
assert r1.value == 0
assert r2.value == 1
assert r3.value == 0

"""
expected
0
0
1
0
"""
