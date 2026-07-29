"""Stage 1 of the headless path: ggl.view turns a .ggc dict into a GGL program string.

These tests exercise the transform two ways: structurally (the emitted text contains the
expected declarations/connections) and behaviorally (exec the emitted program and check
the circuit computes the right answer). The behavioral check is the real proof that a
generated program is both syntactically valid and semantically correct.
"""

from ggl import view


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
