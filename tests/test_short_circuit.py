"""Two wires into one input port is a short circuit and must raise."""

import pytest

from ggl import circuit, io, logic
from ggl.errors import CircuitError


def test_second_wire_into_input_raises():
    c = circuit.Circuit(circuit_name="demo")
    a = io.Input(bits=1, label="A")
    b = io.Input(bits=1, label="B")
    gate = logic.And(js_id="and_1")

    c.connect(a, gate.input("0"))
    with pytest.raises(CircuitError) as exc:
        c.connect(b, gate.input("0"))

    err = exc.value
    assert err.error_code == "inputShortCircuit"
    assert err.component_id == "and_1"
    assert err.port_name == "0"


def test_distinct_ports_are_fine():
    c = circuit.Circuit()
    a = io.Input(bits=1, label="A")
    b = io.Input(bits=1, label="B")
    gate = logic.And()

    # Two wires to two different input ports is normal fan-in, not a short.
    c.connect(a, gate.input("0"))
    c.connect(b, gate.input("1"))
