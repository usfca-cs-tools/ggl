"""Circuit.run()/run_async() preflight every registered node before settling: an
unconnected input port raises CircuitError(inputNotConnected) up front, so an open
input fails at the engine (through the same error path as reading one) rather than
silently or only mid-propagation. Fully-orphaned components (never connect()ed) are
not in all_nodes and aren't reached here — that's the codegen-registration follow-on.
"""

import pytest

from ggl import circuit, io, logic, plexers
from ggl.errors import CircuitError


def test_open_gate_input_is_flagged_by_preflight():
    c = circuit.Circuit()
    a = io.Input(bits=1, label="A"); a.value = 1
    g = logic.And()
    c.connect(a, g.input("0"))            # only one of the AND's two inputs wired
    c.connect(g, io.Output(bits=1, label="Y", js_id="y"))
    with pytest.raises(CircuitError) as ei:
        c.run()
    d = ei.value.to_dict()
    assert d["error_code"] == "inputNotConnected"
    assert d["port_name"] == "1"          # the open input


def test_fully_connected_circuit_preflights_clean():
    c = circuit.Circuit()
    a = io.Input(bits=1, label="A"); a.value = 1
    b = io.Input(bits=1, label="B"); b.value = 1
    g = logic.And()
    c.connect(a, g.input("0"))
    c.connect(b, g.input("1"))
    c.connect(g, io.Output(bits=1, label="Y", js_id="y"))
    c.run()                               # must not raise
    y = next(o for o in c.outputs if o.label == "Y")
    assert y.value == 1


def test_orphaned_output_is_flagged_via_add_orphan():
    # An Output wired to nothing never reaches connect(), so the codegen declares
    # it with add_orphan(); preflight must then flag its open input.
    c = circuit.Circuit()
    a = io.Input(bits=1, label="A"); a.value = 1
    g = logic.Not()
    c.connect(a, g.input("0"))
    c.connect(g, io.Output(bits=1, label="Y", js_id="y"))
    orphan = io.Output(bits=1, label="IW", js_id="iw")   # dropped wire: no connect()
    c.add_orphan(orphan)
    with pytest.raises(CircuitError) as ei:
        c.run()
    d = ei.value.to_dict()
    assert d["error_code"] == "inputNotConnected"
    assert d["component_label"] == "IW"


def test_orphaned_input_source_preflights_clean():
    # An unconnected Input is a source that reads nothing — legitimately unused,
    # so add_orphan()'ing it must NOT raise (no input ports to be open).
    c = circuit.Circuit()
    a = io.Input(bits=1, label="A"); a.value = 1
    c.connect(a, io.Output(bits=1, label="Y", js_id="y"))
    c.add_orphan(io.Input(bits=1, label="UNUSED", js_id="u"))
    c.run()   # must not raise


def test_decoder_has_no_phantom_data_inputs():
    # Regression: a Decoder used to inherit 2 unused data inputs from Plexer;
    # only 'sel' is required, so preflight must pass with just 'sel' connected.
    c = circuit.Circuit()
    sel = io.Input(bits=2, label="SEL"); sel.value = 2
    dec = plexers.Decoder(selector_bits=2, label="DEC")
    c.connect(sel, dec.input("sel"))
    c.connect(dec.output("2"), io.Output(bits=1, label="r2", js_id="r2"))
    c.run()                               # preflight must not flag phantom inputs
    r2 = next(o for o in c.outputs if o.label == "r2")
    assert r2.value == 1
