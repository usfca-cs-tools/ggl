"""ggl.view — pure transform from a Golden Gates ``.ggc`` circuit (a JSON dict) to a
GGL program (a source string).

This module builds NO circuit and runs NOTHING. It only emits text. The caller execs
the returned program separately (in the browser that is a second Pyodide pass; headless
it is a plain ``exec``), which keeps the two stages independently testable:

    stage 1  (this module)   .ggc dict  -> GGL source        [pure, unit-tested here]
    stage 2  (the caller)    exec(source) -> a run Circuit    [the existing engine]

Connectivity is resolved purely from geometry: each wire endpoint coordinate is matched
against the absolute port coordinates carried in the ``.ggc`` (component position + the
serialized per-port grid offset, v1.4+). ggl.view therefore holds no layout/rotation
knowledge of its own — the front end already baked that into the stored port coordinates.
"""

import re

# Golden Gates gate `type` -> ggl logic class name.
_GATE_CLASS = {
    "and-gate": "And",
    "or-gate": "Or",
    "not-gate": "Not",
    "nand-gate": "Nand",
    "nor-gate": "Nor",
    "xor-gate": "Xor",
    "xnor-gate": "Xnor",
}


def _esc(s):
    """Escape a string for embedding in a double-quoted Python literal."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _var(comp_id):
    """A stable, valid Python identifier for a component id."""
    return "n_" + re.sub(r"[^0-9a-zA-Z_]", "_", str(comp_id))


def _component_expr(comp):
    """Return (construction_expr, value_init) for a component, or (None, None) if the
    type isn't a runnable node (e.g. a Test directive). value_init is the int to assign
    to ``.value`` after construction (for sources), or None."""
    t = comp.get("type")
    p = comp.get("props", {}) or {}
    label = p.get("label", "")
    bits = p.get("bits", 1)
    js_id = comp.get("id", "")

    if t == "input":
        return (f'io.Input(label="{_esc(label)}", bits={bits}, js_id="{_esc(js_id)}")',
                int(p.get("value", 0) or 0))
    if t == "constant":
        return (f'io.Constant(label="{_esc(label)}", bits={bits}, js_id="{_esc(js_id)}")',
                int(p.get("value", 0) or 0))
    if t == "output":
        return (f'io.Output(label="{_esc(label)}", bits={bits}, js_id="{_esc(js_id)}")', None)
    if t in _GATE_CLASS:
        args = [f'label="{_esc(label)}"']
        if bits != 1:
            args.append(f"bits={bits}")
        if p.get("numInputs") is not None:
            args.append(f'num_inputs={int(p["numInputs"])}')
        inverted = p.get("invertedInputs") or []
        if inverted:
            args.append(f"inverted_inputs={list(inverted)}")
        args.append(f'js_id="{_esc(js_id)}"')
        return (f'logic.{_GATE_CLASS[t]}({", ".join(args)})', None)

    return (None, None)


def _port_index(components, var_of):
    """Map absolute grid coordinate (x, y) -> list of (var, port_name, direction).

    Absolute = component position + the serialized per-port offset — the same sum the
    front end used for the wire endpoint, so exact integer equality resolves them.
    """
    index = {}
    for comp in components:
        var = var_of.get(comp.get("id"))
        if var is None:
            continue
        cx, cy = comp.get("x", 0), comp.get("y", 0)
        for port in comp.get("ports", []) or []:
            key = (cx + port.get("x", 0), cy + port.get("y", 0))
            index.setdefault(key, []).append(
                (var, port.get("name"), port.get("direction"))
            )
    return index


def _match(index, pos, direction):
    """Resolve a wire endpoint to (var, port_name) for a port of the given direction,
    or None if there isn't exactly one."""
    hits = [
        (var, name)
        for (var, name, d) in index.get((pos.get("x"), pos.get("y")), [])
        if d == direction
    ]
    return hits[0] if len(hits) == 1 else None


def generate(ggc, run_call="circuit0.run()"):
    """Transform a ``.ggc`` dict into a GGL program string. Does not execute it.

    ``run_call`` is the trailing statement that actually simulates once the program is
    exec'd — ``circuit0.run()`` for a headless/synchronous run (the default, handy for
    tests), or ``await circuit0.run_async()`` for the browser's free-running clock.
    """
    components = ggc.get("components", []) or []
    wires = ggc.get("wires", []) or []

    lines = [
        "from ggl import arithmetic, circuit, component, io, logic, memory, plexers, wires",
        "circuit0 = circuit.Circuit()",
    ]

    var_of = {}
    value_inits = []
    for comp in components:
        expr, init = _component_expr(comp)
        if expr is None:
            continue  # not a runnable node (e.g. Test) — later slices handle these
        var = _var(comp["id"])
        var_of[comp["id"]] = var
        lines.append(f"{var} = {expr}")
        if init is not None:
            value_inits.append(f"{var}.value = {init}")
    lines.extend(value_inits)

    index = _port_index(components, var_of)
    for wire in wires:
        src = _match(index, wire.get("startConnection", {}).get("pos", {}), "output")
        dst = _match(index, wire.get("endConnection", {}).get("pos", {}), "input")
        if src and dst:
            (svar, sname), (dvar, dname) = src, dst
            js_id = _esc(wire.get("id", ""))
            lines.append(
                f'circuit0.connect({svar}.output("{sname}"), '
                f'{dvar}.input("{dname}"), js_id="{js_id}")'
            )

    lines.append(run_call)
    return "\n".join(lines) + "\n"
