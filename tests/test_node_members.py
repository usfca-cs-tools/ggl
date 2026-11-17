"""Members declared on Node and its subclasses, and how copying treats them."""
import copy

from ggl import circuit, io, logic, wires


def _inverter_template():
    template = circuit.Circuit(circuit_name="inverter")
    a = io.Input(label="a")
    inverter = logic.Not(label="n")
    out = io.Output(label="o")
    template.connect(a.output("0"), inverter.input("0"))
    template.connect(inverter.output("0"), out.input("0"))
    return template


def test_deep_node_count_includes_embedded_subcircuits():
    top = circuit.Circuit()
    a = io.Input(label="a")
    instance = circuit.Component(_inverter_template())()
    top.connect(a.output("0"), instance.input("a"))
    # The Input, the CircuitNode, and the three nodes inside it
    assert top.deep_node_count() == 5


def test_tunnel_sinks_finds_output_tunnels_by_label():
    top = circuit.Circuit()
    a = io.Input(label="a")
    publisher = wires.Tunnel(label="t", direction="input")
    subscriber = wires.Tunnel(label="t", direction="output")
    other = wires.Tunnel(label="u", direction="output")
    out = io.Output(label="o")
    other_out = io.Output(label="p")
    top.connect(a.output("0"), publisher.input("0"))
    top.connect(subscriber.output("0"), out.input("0"))
    top.connect(other.output("0"), other_out.input("0"))
    assert top.tunnel_sinks("t") == [subscriber]
    assert top.tunnel_sinks("missing") == []


def test_every_node_starts_without_an_owner():
    assert logic.And(label="g").owner is None


def test_connect_sets_owner_on_a_top_level_input():
    top = circuit.Circuit()
    a = io.Input(label="a")
    out = io.Output(label="o")
    top.connect(a.output("0"), out.input("0"))
    assert a.owner is top


def test_deepcopy_of_a_node_drops_its_owner():
    top = circuit.Circuit()
    a = io.Input(label="a")
    out = io.Output(label="o")
    top.connect(a.output("0"), out.input("0"))
    assert copy.deepcopy(a).owner is None


def test_deepcopy_of_a_circuit_node_does_not_walk_its_subcircuit():
    # A deepcopy reaches a CircuitNode through an edge when a nested ChildInput is cloned.
    # Copying the embedded circuit there would walk the whole hierarchy.
    instance = circuit.Component(_inverter_template())()
    assert instance.subcircuit is not None
    assert copy.deepcopy(instance).subcircuit is None
