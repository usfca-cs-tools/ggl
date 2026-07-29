"""ggl.view — pure transform from a Golden Gates ``.ggc`` circuit (a JSON dict) to a
GGL program (a source string).

This module builds NO circuit and runs NOTHING. It only emits text. The caller execs
the returned program separately (in the browser that is a second Pyodide pass; headless
it is a plain ``exec``), which keeps the two stages independently testable:

    stage 1  (this module)   .ggc dict  -> GGL source        [pure, unit-tested here]
    stage 2  (the caller)    exec(source) -> a run Circuit    [the existing engine]

Connectivity is resolved purely from geometry: each wire endpoint coordinate is matched
against the absolute port coordinates carried in the ``.ggc`` (component position + the
serialized per-port grid offset, v1.4+). Coordinates are rounded before matching so the
non-integer port positions some components use compare reliably. Wire *junctions* are
ignored: they are visual routing only — fan-out is expressed as multiple port-to-port
wires, so per-wire endpoint matching already captures every connection.

Hierarchy: a ``schematic-component`` references a saved subcircuit in
``schematicComponents``. Each used subcircuit is emitted once as a ``circuit.Component``
template (recursively, deepest first) and instantiated per placement. A subcircuit's
external ports are the inner circuit's Input/Output *labels* (see ggl.component.CircuitNode),
so a placement's positional port ("0","1",...) is mapped to the inner label by position.
"""

import re

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
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _var(comp_id):
    return "n_" + re.sub(r"[^0-9a-zA-Z_]", "_", str(comp_id))


def _san(s):
    return re.sub(r"[^0-9a-zA-Z_]", "_", str(s))


def _r(v):
    """Round a coordinate for tolerant matching (some ports sit on thirds of a grid)."""
    return round(float(v), 3)


def _component_expr(comp):
    """Return (construction_expr, value_init) for a leaf (non-subcircuit) component, or
    (None, None) if the type isn't a runnable node. value_init is the int to assign to
    ``.value`` after construction (sources), else None."""
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
    if t == "splitter":
        splits = [(rg.get("start"), rg.get("end")) for rg in (p.get("ranges") or [])]
        return (f'wires.Splitter(label="{_esc(label)}", bits={p.get("inputBits", 1)}, '
                f'splits={splits}, js_id="{_esc(js_id)}")', None)
    if t == "merger":
        merges = [(rg.get("start"), rg.get("end")) for rg in (p.get("ranges") or [])]
        return (f'wires.Merger(label="{_esc(label)}", bits={p.get("outputBits", 1)}, '
                f'merge_inputs={merges}, js_id="{_esc(js_id)}")', None)

    return (None, None)


def _inner_labels(subdefs, circuit_id, direction):
    """Ordered inner Input/Output labels of a subcircuit — the port names its
    CircuitNode exposes."""
    inner = (subdefs.get(circuit_id, {}).get("circuit", {}) or {}).get("components", []) or []
    want = "input" if direction == "input" else "output"
    return [c.get("props", {}).get("label", "") for c in inner if c.get("type") == want]


def _port_index(components, var_of, subdefs):
    """Map rounded absolute coordinate (x, y) -> list of (var, port_name, direction).
    For a schematic-component the positional port name is translated to the inner
    circuit's label so it matches the CircuitNode's port."""
    index = {}
    for comp in components:
        var = var_of.get(comp.get("id"))
        if var is None:
            continue
        cx, cy = comp.get("x", 0), comp.get("y", 0)
        is_sch = comp.get("type") == "schematic-component"
        cid = (comp.get("props", {}) or {}).get("circuitId") if is_sch else None
        for port in comp.get("ports", []) or []:
            direction = port.get("direction")
            name = port.get("name")
            if is_sch and cid in subdefs:
                labels = _inner_labels(subdefs, cid, direction)
                try:
                    name = labels[int(port.get("name"))]
                except (ValueError, TypeError, IndexError):
                    pass  # fall back to the raw name
            key = (_r(cx + port.get("x", 0)), _r(cy + port.get("y", 0)))
            index.setdefault(key, []).append((var, name, direction))
    return index


def _match(index, pos, direction):
    hits = [
        (var, name)
        for (var, name, d) in index.get((_r(pos.get("x")), _r(pos.get("y"))), [])
        if d == direction
    ]
    return hits[0] if len(hits) == 1 else None


def _emit_body(components, wires, circ_var, subdefs, templates, lines, is_top):
    """Emit node declarations and connect() calls for one circuit (top level or a
    subcircuit body) into ``circ_var``."""
    var_of = {}
    value_inits = []
    for comp in components:
        t = comp.get("type")
        if t == "schematic-component":
            cid = (comp.get("props", {}) or {}).get("circuitId")
            tvar = templates.get(cid)
            if tvar is None:
                continue  # unknown/undefined subcircuit
            var = _var(comp["id"])
            var_of[comp["id"]] = var
            lines.append(f"{var} = {tvar}()")
            continue
        expr, init = _component_expr(comp)
        if expr is None:
            continue
        var = _var(comp["id"])
        var_of[comp["id"]] = var
        lines.append(f"{var} = {expr}")
        # A subcircuit's own Input nodes are interface ports driven by the parent, so
        # only set a value for top-level inputs; constants always carry their value.
        if init is not None and (t != "input" or is_top):
            value_inits.append(f"{var}.value = {init}")
    lines.extend(value_inits)

    index = _port_index(components, var_of, subdefs)
    seen = set()
    for wire in wires:
        src = _match(index, wire.get("startConnection", {}).get("pos", {}), "output")
        dst = _match(index, wire.get("endConnection", {}).get("pos", {}), "input")
        if not (src and dst):
            continue
        if (src, dst) in seen:
            continue
        seen.add((src, dst))
        (svar, sname), (dvar, dname) = src, dst
        lines.append(
            f'{circ_var}.connect({svar}.output("{sname}"), '
            f'{dvar}.input("{dname}"), js_id="{_esc(wire.get("id", ""))}")'
        )


def generate(ggc, run_call="circuit0.run()"):
    """Transform a ``.ggc`` dict into a GGL program string. Does not execute it.

    ``run_call`` is the trailing statement that simulates once the program is exec'd:
    ``circuit0.run()`` for a synchronous headless run (default, handy for tests), or
    ``await circuit0.run_async()`` for the browser's free-running clock.
    """
    subdefs = ggc.get("schematicComponents", {}) or {}
    lines = [
        "from ggl import arithmetic, circuit, component, io, logic, memory, plexers, wires"
    ]

    templates = {}  # circuitId -> template variable name

    def ensure_template(circuit_id):
        if circuit_id in templates:
            return templates[circuit_id]
        if circuit_id not in subdefs:
            return None
        inner = subdefs[circuit_id].get("circuit", {}) or {}
        for child in inner.get("components", []) or []:
            if child.get("type") == "schematic-component":
                ensure_template((child.get("props", {}) or {}).get("circuitId"))
        cvar = f"sub_{_san(circuit_id)}"
        lines.append("")
        lines.append(f"# subcircuit: {subdefs[circuit_id].get('definition', {}).get('name', circuit_id)}")
        lines.append(f"{cvar} = circuit.Circuit()")
        _emit_body(inner.get("components", []) or [], inner.get("wires", []) or [],
                   cvar, subdefs, templates, lines, is_top=False)
        tvar = f"Tmpl_{_san(circuit_id)}"
        lines.append(f"{tvar} = circuit.Component({cvar})")
        templates[circuit_id] = tvar
        return tvar

    for comp in ggc.get("components", []) or []:
        if comp.get("type") == "schematic-component":
            ensure_template((comp.get("props", {}) or {}).get("circuitId"))

    lines.append("")
    lines.append("circuit0 = circuit.Circuit()")
    _emit_body(ggc.get("components", []) or [], ggc.get("wires", []) or [],
               "circuit0", subdefs, templates, lines, is_top=True)
    lines.append(run_call)
    return "\n".join(lines) + "\n"
