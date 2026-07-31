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


@pytest.mark.parametrize("comp,expected_kind", [
    ({"type": "adder", "props": {"label": "+", "bits": 8}}, "Adder"),
    ({"type": "subtract", "props": {"label": "-", "bits": 8}}, "Subtract"),
    ({"type": "multiply", "props": {"label": "x", "bits": 8}}, "Multiply"),
    ({"type": "divide", "props": {"label": "/", "bits": 8}}, "Division"),
    ({"type": "compare", "props": {"label": "=", "bits": 8}}, "Comparator"),
    ({"type": "shift", "props": {"label": "<<", "bits": 8, "mode": "arithmetic_right"}}, "BarrelShifter"),
    ({"type": "multiplexer", "props": {"label": "M", "selectorBits": 2, "bits": 4}}, "Multiplexer"),
    ({"type": "decoder", "props": {"label": "D", "selectorBits": 3}}, "Decoder"),
    ({"type": "priorityEncoder", "props": {"label": "PE", "selectorBits": 2}}, "PriorityEncoder"),
    ({"type": "register", "props": {"label": "R", "bits": 8}}, "Register"),
    ({"type": "rom", "props": {"label": "ROM", "addressBits": 3, "dataBits": 8, "data": [1, 2, 3]}}, "ROM"),
    ({"type": "ram", "props": {"label": "RAM", "addressBits": 4, "dataBits": 8}}, "RAM"),
    ({"type": "tunnel", "props": {"label": "T", "bits": 4, "direction": "output"}}, "Tunnel"),
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
