"""Directory-aware project loading for headless grading.

`load_project` mirrors how the app opens a project folder: a circuit's subcircuit references
(a schematic-component's `props.filename`) are resolved against sibling `.ggc` files and inlined
into one self-contained model. These tests build a tiny two-file project — a pass-through
subcircuit placed in a top circuit — and grade it straight from its directory, the exact path
`ggl-grade <circuit> --test <t>` takes.
"""

import json

import pytest

from ggl import grade
from ggl.project import ProjectError, load_project


def _port(name, x, y, direction):
    return {"name": name, "x": x, "y": y, "direction": direction}


def _wire(x1, y1, x2, y2, bits=8):
    return {
        "points": [{"x": x1, "y": y1}, {"x": x2, "y": y2}],
        "startConnection": {"pos": {"x": x1, "y": y1}},
        "endConnection": {"pos": {"x": x2, "y": y2}},
        "bits": bits,
    }


def _passthrough_file():
    """A subcircuit .ggc: inputs A,B (8-bit) -> outputs S=A, T=B (two straight wires inside)."""
    return {
        "name": "passthrough",
        "components": [
            {"id": "A", "type": "input", "x": 0, "y": 0,
             "props": {"label": "A", "bits": 8}, "ports": [_port("0", 1, 0, "output")]},
            {"id": "B", "type": "input", "x": 0, "y": 4,
             "props": {"label": "B", "bits": 8}, "ports": [_port("0", 1, 0, "output")]},
            {"id": "S", "type": "output", "x": 10, "y": 0,
             "props": {"label": "S", "bits": 8}, "ports": [_port("0", 0, 0, "input")]},
            {"id": "T", "type": "output", "x": 10, "y": 4,
             "props": {"label": "T", "bits": 8}, "ports": [_port("0", 0, 0, "input")]},
        ],
        "wires": [_wire(1, 0, 10, 0), _wire(1, 4, 10, 4)],
        "wireJunctions": [],
    }


def _top_file(with_test=True):
    """Top .ggc placing the subcircuit by FILENAME (no circuitId — as the project save writes it),
    with the placement's ports already serialized at their real positions (A,B in / S,T out)."""
    inst = {
        "id": "u", "type": "schematic-component", "x": 0, "y": 0,
        "props": {"filename": "passthrough.ggc", "label": "U"},
        "ports": [
            _port("0", 10, 1, "input"), _port("1", 10, 2, "input"),    # A, B
            _port("0", 16, 1, "output"), _port("1", 16, 2, "output"),  # S, T
        ],
    }
    src_a = {"id": "srcA", "type": "input", "x": 0, "y": 0,
             "props": {"label": "SA", "bits": 8}, "ports": [_port("0", 2, 1, "output")]}
    src_b = {"id": "srcB", "type": "input", "x": 0, "y": 0,
             "props": {"label": "SB", "bits": 8}, "ports": [_port("0", 2, 2, "output")]}
    out_s = {"id": "oS", "type": "output", "x": 0, "y": 0,
             "props": {"label": "OS", "bits": 8}, "ports": [_port("0", 20, 1, "input")]}
    out_t = {"id": "oT", "type": "output", "x": 0, "y": 0,
             "props": {"label": "OT", "bits": 8}, "ports": [_port("0", 20, 2, "input")]}
    components = [src_a, src_b, inst, out_s, out_t]
    wires = [_wire(2, 1, 10, 1), _wire(2, 2, 10, 2), _wire(16, 1, 20, 1), _wire(16, 2, 20, 2)]
    if with_test:
        components.append({"id": "test", "type": "test", "ports": [], "props": {
            "label": "pt",
            "table": {"inputNames": ["SA", "SB"], "outputNames": ["OS", "OT"],
                      "rows": [[1, 2, 1, 2]]}}})
    return {"name": "top", "components": components, "wires": wires,
            "wireJunctions": [], "schematicComponents": {}}


def _write_project(dir_path, top_with_test=True, include_sub=True):
    if include_sub:
        (dir_path / "passthrough.ggc").write_text(json.dumps(_passthrough_file()))
    (dir_path / "top.ggc").write_text(json.dumps(_top_file(with_test=top_with_test)))
    return str(dir_path / "top.ggc")


def test_load_project_inlines_referenced_subcircuit(tmp_path):
    model = load_project(_write_project(tmp_path))
    assert model["schematicComponents"], "no subcircuits inlined"
    inst = next(c for c in model["components"] if c["type"] == "schematic-component")
    cid = inst["props"]["circuitId"]  # stamped from the filename
    assert cid in model["schematicComponents"]
    assert model["schematicComponents"][cid]["circuit"]["components"], "subcircuit body missing"


def test_missing_subcircuit_raises(tmp_path):
    top = _write_project(tmp_path, top_with_test=False, include_sub=False)
    with pytest.raises(ProjectError):
        load_project(top)


def test_grade_project_straight_from_directory(tmp_path):
    model = load_project(_write_project(tmp_path))
    ok, message = grade.grade(model)
    assert ok, message
