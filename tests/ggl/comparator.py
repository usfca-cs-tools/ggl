from ggl import circuit
from ggl import io
from ggl import arithmetic


c = circuit.Circuit()

a = io.Input(bits=4, label="a")
b = io.Input(bits=4, label="b")
a.value = 9     
b.value = 6     

comp = arithmetic.Comparator(bits=4, label="multiply")

greater = io.Output(bits=1, label="greater")
equal = io.Output(bits=1, label="equal")
less = io.Output(bits=1, label="less")


c.connect(a, comp.input('a'))
c.connect(b, comp.input('b'))
c.connect(comp.output('gt'),greater)
c.connect(comp.output('eq'),equal)
c.connect(comp.output('lt'),less)


c.run()
assert greater.value == 1
assert equal.value == 0
assert less.value == 0

a.value = 6
b.value = 9

c.run()
assert greater.value == 0
assert equal.value == 0
assert less.value == 1

a.value = 6
b.value = 6

c.run()
assert greater.value == 0
assert equal.value == 1
assert less.value == 0
