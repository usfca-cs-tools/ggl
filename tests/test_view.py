"""Stage 1 of the headless path: ggl.view turns a .ggc dict into a GGL program string.

These tests exercise the transform two ways: structurally (the emitted text contains the
expected declarations/connections) and behaviorally (exec the emitted program and check
the circuit computes the right answer). The behavioral check is the real proof that a
generated program is both syntactically valid and semantically correct.
"""

import json
import os

import pytest

from ggl import view

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "ggc")


def _load(name):
    with open(os.path.join(FIXTURE_DIR, name)) as f:
        return json.load(f)


def _set_input(ggc, label, value):
    for c in ggc["components"]:
        if c.get("type") == "input" and (c.get("props") or {}).get("label") == label:
            c.setdefault("props", {})["value"] = value


def _and_ggc(a, b):
    """A minimal v1.4 circuit: inputs A,B -> 2-input AND -> output Y. Port offsets match
    the front-end registry (input output at (1,0); and-gate inputs (0,0)/(0,2), output
    (3,1); output input at (0,0)). Wire endpoints equal the absolute port coordinates."""
    return {
        "version": "1.4",
        "components": [
            {"id": "A", "type": "input", "x": 0, "y": 0,
             "props": {"label": "A", "bits": 1, "value": a},
             "ports": [{"name": "0", "x": 1, "y": 0, "direction": "output"}]},
            {"id": "B", "type": "input", "x": 0, "y": 4,
             "props": {"label": "B", "bits": 1, "value": b},
             "ports": [{"name": "0", "x": 1, "y": 0, "direction": "output"}]},
            {"id": "G", "type": "and-gate", "x": 5, "y": 0,
             "props": {"label": "g", "bits": 1, "numInputs": 2},
             "ports": [
                 {"name": "0", "x": 0, "y": 0, "direction": "input"},
                 {"name": "1", "x": 0, "y": 2, "direction": "input"},
                 {"name": "0", "x": 3, "y": 1, "direction": "output"},
             ]},
            {"id": "Y", "type": "output", "x": 10, "y": 1,
             "props": {"label": "Y", "bits": 1},
             "ports": [{"name": "0", "x": 0, "y": 0, "direction": "input"}]},
        ],
        "wires": [
            {"id": "w1", "startConnection": {"pos": {"x": 1, "y": 0}, "portType": "output"},
             "endConnection": {"pos": {"x": 5, "y": 0}, "portType": "input"}},
            {"id": "w2", "startConnection": {"pos": {"x": 1, "y": 4}, "portType": "output"},
             "endConnection": {"pos": {"x": 5, "y": 2}, "portType": "input"}},
            {"id": "w3", "startConnection": {"pos": {"x": 8, "y": 1}, "portType": "output"},
             "endConnection": {"pos": {"x": 10, "y": 1}, "portType": "input"}},
        ],
    }


def _run(src):
    ns = {}
    exec(compile(src, "<ggl.view>", "exec"), ns)  # noqa: S102 - our own generated code
    return ns


def _output(ns, label):
    return next(o for o in ns["circuit0"].outputs if o.label == label)


def test_generated_program_is_structurally_sane():
    src = view.generate(_and_ggc(1, 1))
    assert "circuit0 = circuit.Circuit()" in src
    assert "logic.And(" in src
    assert src.count("circuit0.connect(") == 3  # A->G, B->G, G->Y
    assert src.rstrip().endswith("circuit0.run()")


def test_and_true():
    ns = _run(view.generate(_and_ggc(1, 1)))
    assert _output(ns, "Y").value == 1


def test_and_false():
    ns = _run(view.generate(_and_ggc(1, 0)))
    assert _output(ns, "Y").value == 0


@pytest.mark.parametrize("a,b", [(0, 0), (5, 3), (1, 1), (15, 1), (9, 9), (15, 15)])
def test_adder_4_bit_hierarchical(a, b):
    # Real app-saved v1.4 fixture: a 4-bit adder built from four adder_1_bit subcircuits
    # (schematic-components) + splitters/merger/constant. Exercises hierarchy, the
    # schematic port->inner-label mapping, wires emitters, and non-integer gate ports.
    ggc = _load("adder_4_bit.ggc")
    _set_input(ggc, "A", a)
    _set_input(ggc, "B", b)
    ns = _run(view.generate(ggc))
    circ = ns["circuit0"]
    total = a + b
    assert _output(ns, "SUM").value == (total & 0xF)
    assert _output(ns, "COUT").value == (total >> 4)


def _and_with_test(rows):
    ggc = _and_ggc(0, 0)  # base AND circuit; the Test drives A/B itself
    ggc["components"].append({
        "id": "T", "type": "test", "x": 20, "y": 10, "ports": [],
        "props": {"label": "AND",
                  "table": {"inputNames": ["A", "B"], "outputNames": ["Y"], "rows": rows}},
    })
    return ggc


def test_combinational_test_directive_self_checks():
    # mode="test" emits io.Test(...) + test.evaluate(circuit0). A correct truth table
    # passes (evaluate raises nothing); a wrong expected value raises testFailed.
    _run(view.generate(_and_with_test([[0, 0, 0], [0, 1, 0], [1, 0, 0], [1, 1, 1]]), mode="test"))
    from ggl.errors import CircuitError
    with pytest.raises(CircuitError) as ei:
        _run(view.generate(_and_with_test([[1, 1, 0]]), mode="test"))  # AND(1,1)=1, not 0
    assert ei.value.error_code == "testFailed"


def test_sequential_circuit_self_checks_via_clocked_test():
    # The counter has no fixed input->output map; a clocked Test resets it (CLR pulse),
    # sets EN=1, cycles the clock until COUNT reaches 5, then checks COUNT == 5. This
    # exercises the whole sequential path headlessly through ggl.view (emit + evaluate).
    ggc = _load("counter_4_bit.ggc")
    ggc["components"].append({
        "id": "T", "type": "test", "x": 0, "y": 0, "ports": [],
        "props": {
            "label": "count",
            "table": {"inputNames": ["EN"], "outputNames": ["COUNT"], "rows": [[1, 5]]},
            "reset_enabled": True, "reset_input_name": "CLR",
            "stop_enabled": True, "stop_output_name": "COUNT", "stop_output_value": 5,
        },
    })
    _run(view.generate(ggc, mode="test"))  # no raise => counter reached 5 and COUNT == 5


def test_test_async_mode_awaits_cooperative_evaluate():
    # The browser path (mode="test_async") awaits evaluate_async so a long clocked Test
    # yields to the event loop; the headless "test" path stays synchronous.
    src_async = view.generate(_and_with_test([[1, 1, 1]]), mode="test_async")
    assert ".evaluate_async(circuit0)" in src_async
    assert "await " in src_async

    src_sync = view.generate(_and_with_test([[1, 1, 1]]), mode="test")
    assert ".evaluate(circuit0)" in src_sync
    assert "evaluate_async" not in src_sync
    assert "await " not in src_sync


def test_run_tail_selection():
    ggc = _and_ggc(1, 1)
    assert view.generate(ggc, mode="run").rstrip().endswith("circuit0.run()")
    assert view.generate(ggc, mode="run_async").rstrip().endswith("await circuit0.run_async()")


def test_test_emitter_builds_a_valid_io_test():
    expr, _ = view._component_expr({"id": "t", "type": "test", "props": {
        "label": "c", "table": {"inputNames": ["A", "B"], "outputNames": ["Y"], "rows": [[1, 1, 1]]},
        "stop_enabled": True, "stop_output_name": "COUNT", "stop_output_value": 5,
        "reset_enabled": True, "reset_input_name": "CLR"}})
    from ggl import io
    ns = {"io": io}
    exec(f"t = {expr}", ns)  # noqa: S102
    t = ns["t"]
    assert t.kind == "Test"
    assert t.input_names == ["A", "B"] and t.output_names == ["Y"]
    assert t.stop_enabled and t.stop_output_value == 5 and t.reset_input_name == "CLR"


def test_counter_4_bit_counts_when_clocked():
    # Real app-saved v1.4 fixture: a 4-bit counter nesting register -> D flip-flop ->
    # D-latch -> SR-latch (feedback), with junctions for fan-out and an internal constant.
    # Exercises the whole sequential hierarchical path: it must reset on CLR and then
    # increment on each clock edge.
    ggc = _load("counter_4_bit.ggc")
    ns = _run(view.generate(ggc))  # mode="run": settle once, then we drive the clock
    circ = ns["circuit0"]

    def set_input(label, value):
        for n in circ.inputs:
            if n.label == label:
                n.value = value

    count = _output(ns, "COUNT")
    set_input("CLR", 1)
    circ.cycle()
    assert count.value == 0  # asynchronous clear

    set_input("CLR", 0)
    set_input("EN", 1)
    seq = []
    for _ in range(6):
        circ.cycle()
        seq.append(count.value)
    assert seq == [1, 2, 3, 4, 5, 6]


def test_unconnected_subcircuit_output_raises_clear_error():
    # A subcircuit output that isn't wired inside never enters the circuit's interface, so
    # the Component lacks that port and the PARENT's connect to it would blow up deep in the
    # engine with a bare KeyError. view.generate must instead raise a clear CircuitError
    # naming the circuit and port, at generation time. Here 'Y' is left unconnected (the
    # gate->Y wire is omitted); 'A' feeds both gate inputs so only 'Y' is the problem.
    sub = {
        "components": [
            {"id": "A", "type": "input", "x": 0, "y": 0, "props": {"label": "A", "bits": 1},
             "ports": [{"name": "0", "x": 1, "y": 0, "direction": "output"}]},
            {"id": "G", "type": "and-gate", "x": 5, "y": 0, "props": {"label": "g", "numInputs": 2},
             "ports": [{"name": "0", "x": 0, "y": 0, "direction": "input"},
                       {"name": "1", "x": 0, "y": 2, "direction": "input"},
                       {"name": "0", "x": 3, "y": 1, "direction": "output"}]},
            {"id": "Y", "type": "output", "x": 10, "y": 1, "props": {"label": "Y", "bits": 1},
             "ports": [{"name": "0", "x": 0, "y": 0, "direction": "input"}]},
        ],
        "wires": [
            {"id": "w1", "startConnection": {"pos": {"x": 1, "y": 0}}, "endConnection": {"pos": {"x": 5, "y": 0}}},
            {"id": "w2", "startConnection": {"pos": {"x": 1, "y": 0}}, "endConnection": {"pos": {"x": 5, "y": 2}}},
            # NOTE: no wire from the gate output (8,1) to Y (10,1) — Y is left dangling.
        ],
    }
    ggc = {
        "version": "1.4",
        "components": [
            {"id": "S", "type": "schematic-component", "x": 0, "y": 0,
             "props": {"label": "sub", "circuitId": "sub1"}, "ports": []},
        ],
        "wires": [],
        "schematicComponents": {"sub1": {"definition": {"name": "mysub"}, "circuit": sub}},
    }
    from ggl.errors import CircuitError
    with pytest.raises(CircuitError) as ei:
        view.generate(ggc)
    assert ei.value.error_code == "portNotFullyConnected"
    assert ei.value.circuit_name == "mysub"
    assert ei.value.additional_fields.get("label") == "Y"


@pytest.mark.parametrize("nest,expected", [(False, "main"), (True, "widener")])
def test_runtime_error_names_the_circuit_it_occurred_in(nest, expected):
    # A bit-width mismatch raised while settling must name the circuit it happened in. A
    # subcircuit is flattened into its parent at run time, so the offending node is tagged
    # with its own circuit: a top-level mismatch reports "main", the SAME mismatch moved
    # into a subcircuit reports "widener" (the innermost circuit), not the flattened top.
    def port(n, x, y, d):
        return {"name": n, "x": x, "y": y, "direction": d}

    # A splitter declaring 8 input bits but fed a 4-bit source -> bitWidthMismatch on settle.
    # `source` is the node that feeds the splitter: a self-contained top-level circuit drives
    # it with a constant; the subcircuit exposes an interface input A that the parent feeds.
    def mismatch_body(source, source_out_x):
        return {
            "components": [
                source,
                {"id": "SP", "type": "splitter", "x": 3, "y": 0,
                 "props": {"label": "SP", "inputBits": 8, "ranges": [{"start": 0, "end": 7}]},
                 "ports": [port("0", 0, 0, "input"), port("0", 2, 0, "output")]},
                {"id": "OUT", "type": "output", "x": 8, "y": 0, "props": {"label": "Y", "bits": 8},
                 "ports": [port("0", 0, 0, "input")]},
            ],
            "wires": [
                {"startConnection": {"pos": {"x": source_out_x, "y": 0}}, "endConnection": {"pos": {"x": 3, "y": 0}}},
                {"startConnection": {"pos": {"x": 5, "y": 0}}, "endConnection": {"pos": {"x": 8, "y": 0}}},
            ],
        }

    if nest:
        # Subcircuit "widener": interface input A(4) -> splitter(8) -> output Y; the parent
        # drives A with a 4-bit constant, so the mismatch fires inside the subcircuit.
        sub_in = {"id": "A", "type": "input", "x": 0, "y": 0, "props": {"label": "A", "bits": 4},
                  "ports": [port("0", 1, 0, "output")]}
        ggc = {
            "name": "main", "version": "1.4",
            "schematicComponents": {
                "w": {"definition": {"name": "widener"}, "circuit": mismatch_body(sub_in, 1)}},
            "components": [
                {"id": "K", "type": "constant", "x": -3, "y": 0,
                 "props": {"label": "K", "value": "5", "bits": 4}, "ports": [port("0", 1, 0, "output")]},
                {"id": "SC", "type": "schematic-component", "x": 0, "y": 0,
                 "props": {"label": "W", "circuitId": "w"},
                 "ports": [port("A", 0, 0, "input"), port("Y", 2, 0, "output")]},
                {"id": "TOUT", "type": "output", "x": 8, "y": 0, "props": {"label": "Out", "bits": 8},
                 "ports": [port("0", 0, 0, "input")]},
            ],
            "wires": [
                {"startConnection": {"pos": {"x": -2, "y": 0}}, "endConnection": {"pos": {"x": 0, "y": 0}}},
                {"startConnection": {"pos": {"x": 2, "y": 0}}, "endConnection": {"pos": {"x": 8, "y": 0}}},
            ],
        }
    else:
        const = {"id": "C", "type": "constant", "x": 0, "y": 0,
                 "props": {"label": "C", "value": "5", "bits": 4}, "ports": [port("0", 1, 0, "output")]}
        ggc = {"name": "main", "version": "1.4", **mismatch_body(const, 1)}

    from ggl.errors import CircuitError
    with pytest.raises(CircuitError) as ei:
        exec(compile(view.generate(ggc, mode="run"), "<gen>", "exec"), {})
    assert ei.value.error_code == "bitWidthMismatch"
    assert ei.value.circuit_name == expected


def test_junction_trunk_resolved_by_geometry_not_stale_index():
    # A junction is a T-tap: a branch wire meets a trunk mid-run at `pos`. The trunk must
    # be found by GEOMETRY (which wire passes through `pos`), not the serialized positional
    # `sourceWireIndex`. That index goes stale after edits and can point at a wire in an
    # unrelated net — welding two independent nets into one, so the resolver keeps only the
    # first driver and silently drops the other. This is exactly how a 1-bit-adder subcircuit
    # lost its `B` input (A's fan-out net absorbed B's), which then blew up as KeyError 'B'
    # when the parent tried to drive the vanished port.
    wires = [
        # 0: net A trunk — horizontal at y=0; (5,0) is mid-span, NOT an endpoint.
        {"id": "wA", "startConnection": {"pos": {"x": 1, "y": 0}},
         "endConnection": {"pos": {"x": 9, "y": 0}},
         "points": [{"x": 1, "y": 0}, {"x": 9, "y": 0}]},
        # 1: net A branch tapping the trunk at (5,0) — joined only by the junction.
        {"id": "wAb", "startConnection": {"pos": {"x": 5, "y": 0}},
         "endConnection": {"pos": {"x": 5, "y": 3}},
         "points": [{"x": 5, "y": 0}, {"x": 5, "y": 3}]},
        # 2: net B — a completely separate wire at y=10.
        {"id": "wB", "startConnection": {"pos": {"x": 1, "y": 10}},
         "endConnection": {"pos": {"x": 9, "y": 10}},
         "points": [{"x": 1, "y": 10}, {"x": 9, "y": 10}]},
    ]
    # The junction taps wA at (5,0) with branch wAb — but sourceWireIndex is stale and points
    # at wB (index 2), a wire in the other net. Geometry (pos on wA) must win.
    junctions = [{"pos": {"x": 5, "y": 0}, "connectedWireId": "wAb", "sourceWireIndex": 2}]
    net_of = {}
    for k, net in enumerate(view._group_nets(wires, junctions)):
        for i in net:
            net_of[i] = k
    assert net_of[0] == net_of[1]  # branch joins its real trunk (A), via geometry
    assert net_of[2] != net_of[0]  # net B stays independent despite the stale index


def test_counter_no_subcircuits_counts_when_clocked():
    # The same behavior from a counter built out of ggl CLASSES, not gate-level subcircuits:
    # register -> adder(+1) -> mux -> register, where the mux picks the sum or a constant 0
    # by CLR (a SYNCHRONOUS clear). Complements counter_4_bit's hierarchical path — this one
    # exercises the direct class emitters (Register/Adder/Multiplexer) and their named-port
    # connections (D/CLK/en/Q, sel/0/1, a/b/cin/sum) with no schematicComponents.
    ggc = _load("counter-no-subcircuits.ggc")
    src = view.generate(ggc)
    assert "memory.Register(" in src and "plexers.Multiplexer(" in src  # flat, class-level
    ns = _run(src)
    circ = ns["circuit0"]
    count = _output(ns, "COUNT")

    def set_clr(v):
        for n in circ.inputs:
            if n.label == "CLR":
                n.value = v

    set_clr(1)
    circ.cycle()
    assert count.value == 0  # synchronous clear: CLR=1 loads 0 through the mux on the edge

    set_clr(0)  # EN is a hardwired constant here, so counting just needs the clock
    seq = []
    for _ in range(6):
        circ.cycle()
        seq.append(count.value)
    assert seq == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("comp,expected_kind", [
    ({"type": "adder", "props": {"label": "+", "bits": 8}}, "Adder"),
    ({"type": "subtract", "props": {"label": "-", "bits": 8}}, "Subtract"),
    ({"type": "multiply", "props": {"label": "x", "bits": 8}}, "Multiply"),
    ({"type": "divide", "props": {"label": "/", "bits": 8}}, "Division"),
    ({"type": "compare", "props": {"label": "=", "bits": 8}}, "Comparator"),
    ({"type": "shift", "props": {"label": "<<", "bits": 8, "mode": "arithmetic_right"}}, "BarrelShifter"),
    ({"type": "signExtend", "props": {"label": "SE", "inBits": 8, "outBits": 16}}, "SignExtend"),
    ({"type": "multiplexer", "props": {"label": "M", "selectorBits": 2, "bits": 4}}, "Multiplexer"),
    ({"type": "decoder", "props": {"label": "D", "selectorBits": 3}}, "Decoder"),
    ({"type": "priorityEncoder", "props": {"label": "PE", "selectorBits": 2}}, "PriorityEncoder"),
    ({"type": "register", "props": {"label": "R", "bits": 8}}, "Register"),
    ({"type": "rom", "props": {"label": "ROM", "addressBits": 3, "dataBits": 8, "data": [1, 2, 3]}}, "ROM"),
    ({"type": "ram", "props": {"label": "RAM", "addressBits": 4, "dataBits": 8}}, "RAM"),
    ({"type": "tunnel", "props": {"label": "T", "bits": 4, "direction": "output"}}, "Tunnel"),
    ({"type": "constant", "props": {"label": "K", "bits": 4, "value": 5}}, "Constant"),
    ({"type": "splitter", "props": {"label": "S", "inputBits": 4,
      "ranges": [{"start": 0, "end": 1}, {"start": 2, "end": 3}]}}, "Splitter"),
    ({"type": "merger", "props": {"label": "MG", "outputBits": 4,
      "ranges": [{"start": 0, "end": 1}, {"start": 2, "end": 3}]}}, "Merger"),
])
def test_leaf_emitter_constructs_a_valid_ggl_node(comp, expected_kind):
    # The emitted construction line must actually build the right ggl node — this is
    # what would fail on a bad class name (arithmetic.Divide) or wrong kwargs (Shift).
    expr, _ = view._component_expr({"id": "x", **comp})
    from ggl import arithmetic, circuit, io, logic, memory, plexers, wires
    ns = {"arithmetic": arithmetic, "circuit": circuit, "io": io, "logic": logic,
          "memory": memory, "plexers": plexers, "wires": wires}
    exec(f"n = {expr}", ns)  # noqa: S102 - our own generated line
    assert ns["n"].kind == expected_kind


def test_shift_maps_packed_mode_to_direction_and_mode():
    expr, _ = view._component_expr(
        {"id": "s", "type": "shift", "props": {"label": "s", "bits": 4, "mode": "arithmetic_right"}})
    assert 'direction="right"' in expr and 'mode="arithmetic"' in expr


def test_rom_pads_and_clamps_data_to_address_space():
    expr, _ = view._component_expr(
        {"id": "r", "type": "rom", "props": {"addressBits": 2, "dataBits": 2, "data": [1, 9, 3]}})
    # 2 address bits -> 4 cells; values clamped to 2-bit max (3); missing cells zero.
    assert "data=[1, 3, 3, 0]" in expr


def _decoder_ggc(sel):
    """A 1-selector-bit decoder: input A -> decoder.sel, decoder.output("0") -> output Y.
    Ports are named (sel / 0 / 1), so connectivity must resolve by *name*, not index."""
    return {
        "version": "1.4",
        "components": [
            {"id": "A", "type": "input", "x": 0, "y": 0,
             "props": {"label": "A", "bits": 1, "value": sel},
             "ports": [{"name": "0", "x": 1, "y": 0, "direction": "output"}]},
            {"id": "D", "type": "decoder", "x": 5, "y": 0,
             "props": {"label": "D", "selectorBits": 1},
             "ports": [
                 {"name": "sel", "x": 0, "y": 0, "direction": "input"},
                 {"name": "0", "x": 2, "y": 0, "direction": "output"},
                 {"name": "1", "x": 2, "y": 2, "direction": "output"},
             ]},
            {"id": "Y", "type": "output", "x": 10, "y": 0,
             "props": {"label": "Y", "bits": 1},
             "ports": [{"name": "0", "x": 0, "y": 0, "direction": "input"}]},
        ],
        "wires": [
            {"id": "w1", "startConnection": {"pos": {"x": 1, "y": 0}, "portType": "output"},
             "endConnection": {"pos": {"x": 5, "y": 0}, "portType": "input"}},
            {"id": "w2", "startConnection": {"pos": {"x": 7, "y": 0}, "portType": "output"},
             "endConnection": {"pos": {"x": 10, "y": 0}, "portType": "input"}},
        ],
    }


def test_decoder_named_ports_resolve_and_decode():
    # Re-expresses the deleted Decoder.integration.test.js: the selector and numbered
    # outputs wire by NAME (decoder0.input("sel"), decoder0.output("0")), and the wired
    # program actually decodes. The behavioral half is the proof the named port exists on
    # the node — a bad name would raise at connect() time.
    src0 = view.generate(_decoder_ggc(0))
    assert '.input("sel")' in src0            # sel resolved by name, not positional index
    assert '.output("0")' in src0
    assert _output(_run(src0), "Y").value == 1  # sel=0 lights output "0"
    assert _output(_run(view.generate(_decoder_ggc(1))), "Y").value == 0  # sel=1 -> "0" low


def _tunnel_ggc(value, pub_label="N", sub_label="N", pub_stored="input", sub_stored="output"):
    """Input A -> publisher tunnel -> (virtual net) -> subscriber tunnel -> Output Y.
    The tunnels' *stored* port directions are parameterized so a test can prove ggl.view
    infers direction from wiring rather than trusting the prop."""
    return {
        "version": "1.4",
        "components": [
            {"id": "A", "type": "input", "x": 0, "y": 0,
             "props": {"label": "A", "bits": 1, "value": value},
             "ports": [{"name": "0", "x": 1, "y": 0, "direction": "output"}]},
            {"id": "TP", "type": "tunnel", "x": 3, "y": 0,
             "props": {"label": pub_label, "bits": 1, "direction": pub_stored},
             "ports": [{"name": "0", "x": 0, "y": 0, "direction": pub_stored}]},
            {"id": "TS", "type": "tunnel", "x": 5, "y": 0,
             "props": {"label": sub_label, "bits": 1, "direction": sub_stored},
             "ports": [{"name": "0", "x": 0, "y": 0, "direction": sub_stored}]},
            {"id": "Y", "type": "output", "x": 7, "y": 0,
             "props": {"label": "Y", "bits": 1},
             "ports": [{"name": "0", "x": 0, "y": 0, "direction": "input"}]},
        ],
        "wires": [
            {"id": "w1", "startConnection": {"pos": {"x": 1, "y": 0}, "portType": "output"},
             "endConnection": {"pos": {"x": 3, "y": 0}, "portType": "input"}},
            {"id": "w2", "startConnection": {"pos": {"x": 5, "y": 0}, "portType": "output"},
             "endConnection": {"pos": {"x": 7, "y": 0}, "portType": "input"}},
        ],
    }


@pytest.mark.parametrize("value", [0, 1])
def test_tunnel_net_carries_value_across(value):
    # Two same-label tunnels, no wire between them, form one net: A drives the publisher,
    # the subscriber drives Y. End-to-end proof that generate() wires a virtual net.
    ns = _run(view.generate(_tunnel_ggc(value)))
    assert _output(ns, "Y").value == value


def test_tunnel_direction_is_inferred_not_stored():
    # Both tunnels carry the WRONG stored direction ("input"); ggl.view must infer from
    # wiring — the one fed by A is the publisher (input), the one driving Y is the
    # subscriber (output) — so the net still carries the value.
    src = view.generate(_tunnel_ggc(1, pub_stored="input", sub_stored="input"))
    assert 'direction="input"' in src and 'direction="output"' in src  # inferred, not stored
    assert _output(_run(src), "Y").value == 1


def test_tunnel_net_with_no_driver_raises():
    # A subscriber tunnel drives Y but nothing feeds the net -> tunnelNoDriver.
    from ggl.errors import CircuitError
    ggc = _tunnel_ggc(1)
    ggc["components"] = [c for c in ggc["components"] if c["id"] != "A"]  # remove the driver
    ggc["wires"] = [w for w in ggc["wires"] if w["id"] != "w1"]
    with pytest.raises(CircuitError) as ei:
        view.generate(ggc)
    assert ei.value.error_code == "simulation.errors.tunnelNoDriver"


def test_tunnel_net_with_two_drivers_raises():
    # Two publisher tunnels share a label -> contention -> tunnelMultipleDrivers.
    from ggl.errors import CircuitError
    ggc = _tunnel_ggc(1)
    ggc["components"].append(
        {"id": "B", "type": "input", "x": 0, "y": 4,
         "props": {"label": "B", "bits": 1, "value": 1},
         "ports": [{"name": "0", "x": 1, "y": 0, "direction": "output"}]})
    ggc["components"].append(
        {"id": "TP2", "type": "tunnel", "x": 3, "y": 4,
         "props": {"label": "N", "bits": 1, "direction": "input"},
         "ports": [{"name": "0", "x": 0, "y": 0, "direction": "input"}]})
    ggc["wires"].append(
        {"id": "w3", "startConnection": {"pos": {"x": 1, "y": 4}, "portType": "output"},
         "endConnection": {"pos": {"x": 3, "y": 4}, "portType": "input"}})
    with pytest.raises(CircuitError) as ei:
        view.generate(ggc)
    assert ei.value.error_code == "simulation.errors.tunnelMultipleDrivers"


def test_tunnel_history_is_reset_per_program():
    # tunnel_history is process-global; each generated program clears it at the top so a
    # prior run's tunnels can't leak. Run label "N"=1, then label "N"=0 in the same process:
    # without the reset, the stale publisher would still push 1 to the new subscriber.
    assert "wires.Tunnel.reset_history()" in view.generate(_tunnel_ggc(1))
    assert _output(_run(view.generate(_tunnel_ggc(1))), "Y").value == 1
    assert _output(_run(view.generate(_tunnel_ggc(0))), "Y").value == 0


def test_unresolved_wire_is_skipped_not_crashed():
    # A wire whose endpoint hits no port must be dropped, not raise.
    ggc = _and_ggc(1, 1)
    ggc["wires"].append({
        "id": "dangling",
        "startConnection": {"pos": {"x": 99, "y": 99}, "portType": "output"},
        "endConnection": {"pos": {"x": 98, "y": 98}, "portType": "input"},
    })
    ns = _run(view.generate(ggc))
    assert _output(ns, "Y").value == 1
