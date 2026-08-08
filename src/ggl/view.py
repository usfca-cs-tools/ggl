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

from .errors import (
    CircuitError,
    ERROR_TUNNEL_MULTIPLE_DRIVERS,
    ERROR_TUNNEL_NO_DRIVER,
)

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


def _str_list(names):
    return "[" + ", ".join(f'"{_esc(n)}"' for n in names) + "]"


def _row_list(rows):
    return "[" + ", ".join(
        "[" + ", ".join(str(int(v or 0)) for v in (row or [])) + "]" for row in rows
    ) + "]"


def _san(s):
    return re.sub(r"[^0-9a-zA-Z_]", "_", str(s))


def _r(v):
    """Round a coordinate for tolerant matching (some ports sit on thirds of a grid)."""
    return round(float(v), 3)


def _component_expr(comp, tunnel_dir=None):
    """Return (construction_expr, value_init) for a leaf (non-subcircuit) component, or
    (None, None) if the type isn't a runnable node. value_init is the int to assign to
    ``.value`` after construction (sources), else None. ``tunnel_dir`` overrides a tunnel's
    stored direction with the one inferred from its wiring (see _infer_tunnel_directions)."""
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
    if t == "clock":
        return (f'io.Clock(label="{_esc(label)}", frequency={p.get("frequency", 1)}, '
                f'mode="{_esc(p.get("mode", "manual"))}", js_id="{_esc(js_id)}")', None)
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
    if t == "tunnel":
        # A tunnel's direction is derived from what it's wired to (a tunnel fed by a driver
        # publishes to the net -> input; one that drives a sink reads from it -> output), not
        # from the stored prop. Same-label tunnels are joined inside the engine.
        direction = tunnel_dir if tunnel_dir is not None else p.get("direction", "input")
        return (f'wires.Tunnel(label="{_esc(label)}", '
                f'direction="{_esc(direction)}", js_id="{_esc(js_id)}")', None)

    # --- arithmetic ---
    if t == "adder":
        return (f'arithmetic.Adder(label="{_esc(label)}", bits={bits}, js_id="{_esc(js_id)}")', None)
    if t == "subtract":
        return (f'arithmetic.Subtract(label="{_esc(label)}", bits={bits}, js_id="{_esc(js_id)}")', None)
    if t == "multiply":
        return (f'arithmetic.Multiply(label="{_esc(label)}", bits={bits}, js_id="{_esc(js_id)}")', None)
    if t == "divide":
        # The ggl class is Division (the TS generator emitted a nonexistent 'Divide').
        return (f'arithmetic.Division(label="{_esc(label)}", bits={bits}, js_id="{_esc(js_id)}")', None)
    if t == "compare":
        return (f'arithmetic.Comparator(label="{_esc(label)}", bits={bits}, js_id="{_esc(js_id)}")', None)
    if t == "shift":
        # BarrelShifter wants direction + mode separately; the .ggc packs them into one
        # field like "logical_left" / "arithmetic_right".
        m = str(p.get("mode", "logical_left"))
        direction = "right" if "right" in m else "left"
        shift_mode = "arithmetic" if "arithmetic" in m else "logical"
        return (f'arithmetic.BarrelShifter(label="{_esc(label)}", bits={bits}, '
                f'direction="{direction}", mode="{shift_mode}", js_id="{_esc(js_id)}")', None)
    if t == "signExtend":
        # Distinct input/output widths (in_bits/out_bits), not the shared `bits`.
        in_bits, out_bits = int(p.get("inBits", 1)), int(p.get("outBits", 1))
        return (f'arithmetic.SignExtend(label="{_esc(label)}", in_bits={in_bits}, '
                f'out_bits={out_bits}, js_id="{_esc(js_id)}")', None)

    # --- plexers ---
    if t == "multiplexer":
        args = [f'label="{_esc(label)}"', f'selector_bits={int(p.get("selectorBits", 2))}']
        if bits != 1:
            args.append(f"bits={bits}")
        args.append(f'js_id="{_esc(js_id)}"')
        return (f'plexers.Multiplexer({", ".join(args)})', None)
    if t == "decoder":
        return (f'plexers.Decoder(label="{_esc(label)}", '
                f'selector_bits={int(p.get("selectorBits", 2))}, js_id="{_esc(js_id)}")', None)
    if t == "priorityEncoder":
        return (f'plexers.PriorityEncoder(label="{_esc(label)}", '
                f'selector_bits={int(p.get("selectorBits", 2))}, js_id="{_esc(js_id)}")', None)

    # --- memory ---
    if t == "register":
        return (f'memory.Register(label="{_esc(label)}", bits={bits}, js_id="{_esc(js_id)}")', None)
    if t == "rom":
        ab, db = int(p.get("addressBits", 4)), int(p.get("dataBits", 8))
        maxv, src = (1 << db) - 1, (p.get("data") or [])
        data = [max(0, min(int(src[i]), maxv)) if i < len(src) else 0 for i in range(1 << ab)]
        return (f'memory.ROM(label="{_esc(label)}", address_bits={ab}, data_bits={db}, '
                f'data={data}, js_id="{_esc(js_id)}")', None)
    if t == "ram":
        ab, db = int(p.get("addressBits", 4)), int(p.get("dataBits", 8))
        return (f'memory.RAM(label="{_esc(label)}", address_bits={ab}, data_bits={db}, '
                f'js_id="{_esc(js_id)}")', None)

    # --- verification directive (not a circuit node; has no ports) ---
    if t == "test":
        table = p.get("table") or {}
        args = [
            f'label="{_esc(label)}"',
            f'input_names={_str_list(table.get("inputNames") or [])}',
            f'output_names={_str_list(table.get("outputNames") or [])}',
            f'rows={_row_list(table.get("rows") or [])}',
        ]
        if p.get("stop_enabled"):
            args += [
                "stop_enabled=True",
                f'stop_output_name="{_esc(p.get("stop_output_name", ""))}"',
                f'stop_output_value={int(p.get("stop_output_value") or 0)}',
            ]
        if p.get("reset_enabled"):
            args += [
                "reset_enabled=True",
                f'reset_input_name="{_esc(p.get("reset_input_name", ""))}"',
            ]
        args.append(f'js_id="{_esc(js_id)}"')
        return (f'io.Test({", ".join(args)})', None)

    return (None, None)


def _inner_labels(subdefs, circuit_id, direction):
    """Ordered inner Input/Output labels of a subcircuit — the port names its
    CircuitNode exposes."""
    inner = (subdefs.get(circuit_id, {}).get("circuit", {}) or {}).get("components", []) or []
    want = "input" if direction == "input" else "output"
    return [c.get("props", {}).get("label", "") for c in inner if c.get("type") == want]


def _port_index(components, var_of, subdefs, tunnel_dirs=None):
    """Map rounded absolute coordinate (x, y) -> list of (var, port_name, direction).
    For a schematic-component the positional port name is translated to the inner
    circuit's label so it matches the CircuitNode's port. A tunnel's port direction is
    taken from ``tunnel_dirs`` (inferred from wiring) so the resolver buckets it as
    driver vs sink consistently with the emitted node."""
    tunnel_dirs = tunnel_dirs or {}
    index = {}
    for comp in components:
        var = var_of.get(comp.get("id"))
        if var is None:
            continue
        cx, cy = comp.get("x", 0), comp.get("y", 0)
        is_sch = comp.get("type") == "schematic-component"
        cid = (comp.get("props", {}) or {}).get("circuitId") if is_sch else None
        tunnel_dir = tunnel_dirs.get(comp.get("id")) if comp.get("type") == "tunnel" else None
        for port in comp.get("ports", []) or []:
            direction = tunnel_dir if tunnel_dir is not None else port.get("direction")
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


def _endpoints(wire):
    s, e = wire.get("startConnection", {}).get("pos", {}), wire.get("endConnection", {}).get("pos", {})
    return (_r(s.get("x")), _r(s.get("y"))), (_r(e.get("x")), _r(e.get("y")))


def _wire_path(wire):
    """The wire's routed polyline as rounded (x, y) vertices. Falls back to the two
    endpoints when no explicit ``points`` array is present."""
    pts = wire.get("points")
    if pts:
        return [(_r(p.get("x")), _r(p.get("y"))) for p in pts]
    return list(_endpoints(wire))


def _wire_covers(wire, pt):
    """True when ``pt`` lies on the wire — at a vertex or anywhere along a segment.
    Used to find the trunk a junction taps by geometry rather than a stored index."""
    px, py = pt
    path = _wire_path(wire)
    for (ax, ay), (bx, by) in zip(path, path[1:]):
        # Collinear (zero cross product) and within the segment's bounding box.
        if abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) < 1e-6 and (
            min(ax, bx) - 1e-6 <= px <= max(ax, bx) + 1e-6
            and min(ay, by) - 1e-6 <= py <= max(ay, by) + 1e-6
        ):
            return True
    return False


def _group_nets(wires, junctions):
    """Union-find wires into electrical nets — joined by a shared endpoint coordinate or by
    a junction — and return a list of nets, each a list of wire indices."""
    from collections import defaultdict

    n = len(wires)
    parent = list(range(n))

    def find(i):
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:
            parent[i], i = root, parent[i]
        return root

    def union(a, b):
        parent[find(a)] = find(b)

    coord_wires = defaultdict(list)
    for i, wire in enumerate(wires):
        for key in _endpoints(wire):
            coord_wires[key].append(i)
    for group in coord_wires.values():
        for j in group[1:]:
            union(group[0], j)

    # A junction is a T-tap: a branch wire meets a trunk wire mid-run at ``pos``,
    # joining them into one net. Resolve the trunk (and branch) by GEOMETRY — every
    # wire whose routed path passes through ``pos`` — rather than the serialized
    # positional ``sourceWireIndex``. That index is fragile: after edits/reordering it
    # can point at an unrelated wire and weld two independent nets together (e.g. input
    # A's fan-out net into input B's, silently dropping B as a driver). ``connectedWireId``
    # is a stable id, so include it too in case a branch endpoint rounds off the path.
    id_to_i = {wire.get("id"): i for i, wire in enumerate(wires)}
    for jn in junctions or []:
        pos = jn.get("pos") or {}
        key = (_r(pos.get("x")), _r(pos.get("y")))
        touching = [i for i, wire in enumerate(wires) if _wire_covers(wire, key)]
        ci = id_to_i.get(jn.get("connectedWireId"))
        if ci is not None and ci not in touching:
            touching.append(ci)
        for k in touching[1:]:
            union(touching[0], k)

    nets = defaultdict(list)
    for i in range(n):
        nets[find(i)].append(i)
    return list(nets.values())


def _infer_tunnel_directions(components, wires, junctions):
    """Infer each tunnel's direction from what it's wired to, ignoring the stored prop.
    Returns ``{js_id: "input"|"output"}`` for every *wired* tunnel.

    A tunnel whose local net contains a real driver (a non-tunnel output port) is a
    publisher: it reads that driven value and pushes it onto the named net, so its own port
    is an input. Otherwise the tunnel is a subscriber — it sources the net's value onto a
    local wire, so its port is an output. An unwired tunnel is in no net and is omitted.
    """
    from collections import defaultdict

    port_at = defaultdict(list)  # coord -> [(comp_id, direction, is_tunnel)]
    for comp in components:
        is_tunnel = comp.get("type") == "tunnel"
        cx, cy = comp.get("x", 0), comp.get("y", 0)
        for port in comp.get("ports", []) or []:
            key = (_r(cx + port.get("x", 0)), _r(cy + port.get("y", 0)))
            port_at[key].append((comp.get("id"), port.get("direction"), is_tunnel))

    result = {}
    for net in _group_nets(wires, junctions):
        coords = {key for i in net for key in _endpoints(wires[i])}
        has_driver = any(
            direction == "output" and not is_tunnel
            for key in coords
            for (_cid, direction, is_tunnel) in port_at.get(key, [])
        )
        for key in coords:
            for (cid, _direction, is_tunnel) in port_at.get(key, []):
                if is_tunnel:
                    result[cid] = "input" if has_driver else "output"
    return result


def _validate_tunnel_nets(components, tunnel_dirs):
    """Raise a CircuitError for a tunnel net that cannot work: no driver (every same-label
    tunnel is a subscriber, so they would read nothing) or more than one driver (two
    publishers contend). Only labelled, wired tunnels form a net."""
    from collections import defaultdict

    by_label = defaultdict(lambda: {"publishers": [], "subscribers": []})
    for comp in components:
        if comp.get("type") != "tunnel":
            continue
        direction = tunnel_dirs.get(comp.get("id"))
        if direction is None:
            continue  # unwired tunnel: part of no net
        label = (comp.get("props", {}) or {}).get("label", "")
        if not label:
            continue  # an unlabelled tunnel names no net (the engine ignores it)
        bucket = "publishers" if direction == "input" else "subscribers"
        by_label[label][bucket].append(comp)

    for label, group in by_label.items():
        publishers, subscribers = group["publishers"], group["subscribers"]
        if len(publishers) >= 2:
            bad = publishers[1]
            raise CircuitError(
                component_id=bad.get("id"), component_type="tunnel",
                component_label=label, error_code=ERROR_TUNNEL_MULTIPLE_DRIVERS, label=label)
        if not publishers and subscribers:
            bad = subscribers[0]
            raise CircuitError(
                component_id=bad.get("id"), component_type="tunnel",
                component_label=label, error_code=ERROR_TUNNEL_NO_DRIVER, label=label)


def _resolve_connections(wires, junctions, index):
    """Group wires into electrical nets and return (src, dst, wire_id) connections.

    A net is a maximal set of wires joined either by a shared endpoint coordinate or by
    a junction (a branch wire meeting another wire mid-run). Each net's ports are found
    by matching every endpoint against the port index; the net's one output drives each
    of its inputs. This subsumes both fan-out styles: multiple wires off one output port,
    and junction branches off a trunk wire.
    """
    conns = []
    for net in _group_nets(wires, junctions):
        outs, ins, input_wire = [], [], {}
        for i in net:
            for key in _endpoints(wires[i]):
                for (var, name, direction) in index.get(key, []):
                    port = (var, name)
                    if direction == "output":
                        if port not in outs:
                            outs.append(port)
                    else:
                        if port not in ins:
                            ins.append(port)
                        input_wire.setdefault(port, wires[i].get("id", ""))
        if outs and ins:
            src = outs[0]
            for dst in ins:
                conns.append((src, dst, input_wire.get(dst, "")))
    # Stable output order regardless of union-find internals.
    conns.sort(key=lambda c: (c[0], c[1]))
    return conns


def _validate_interface_connected(components, var_of, conns, circuit_name):
    """A subcircuit's Input/Output nodes are its interface ports. If one isn't wired to
    anything inside the subcircuit it never enters the circuit's inputs/outputs, so the
    Component built from it silently lacks that port — and the *parent's* connect to that
    port then fails deep in the engine with a bare ``KeyError``. Catch it here, at generation
    time, with a clear message naming the circuit and the port. (Only subcircuits: a
    top-level open input/output is a non-fatal 'unused' warning, handled elsewhere.)"""
    driven = {dvar for (_s, (dvar, _dn), _js) in conns}    # ports something drives (a dst)
    driving = {svar for ((svar, _sn), _d, _js) in conns}   # ports that drive (a src)
    for comp in components:
        t = comp.get("type")
        if t not in ("input", "output"):
            continue
        var = var_of.get(comp.get("id"))
        if var is None:
            continue
        connected = var in driving if t == "input" else var in driven
        if not connected:
            label = (comp.get("props", {}) or {}).get("label") or comp.get("id")
            raise CircuitError(
                component_id=comp.get("id"), component_type=t, component_label=label,
                error_code="portNotFullyConnected", circuit_name=circuit_name,
                port_name=label, label=label)


def _emit_body(components, wires, junctions, circ_var, subdefs, templates, lines, is_top,
               circuit_name=None):
    """Emit node declarations and connect() calls for one circuit (top level or a
    subcircuit body) into ``circ_var``."""
    # A tunnel's direction is derived from its wiring (not the stored prop), and same-label
    # tunnels must form a valid net (exactly one driver). Resolve and check both up front.
    tunnel_dirs = _infer_tunnel_directions(components, wires, junctions)
    _validate_tunnel_nets(components, tunnel_dirs)

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
        expr, init = _component_expr(
            comp, tunnel_dir=tunnel_dirs.get(comp.get("id")) if t == "tunnel" else None
        )
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

    index = _port_index(components, var_of, subdefs, tunnel_dirs)
    conns = _resolve_connections(wires, junctions, index)
    if not is_top:
        _validate_interface_connected(components, var_of, conns, circuit_name)
    for (svar, sname), (dvar, dname), js in conns:
        lines.append(
            f'{circ_var}.connect({svar}.output("{sname}"), '
            f'{dvar}.input("{dname}"), js_id="{_esc(js)}")'
        )


def generate(ggc, mode="run"):
    """Transform a ``.ggc`` dict into a GGL program string. Does not execute it.

    ``mode`` selects the trailing statement that simulates once the program is exec'd:

      - ``"run"``       -> ``circuit0.run()``: one synchronous settle. Correct for
                          combinational circuits; the default, and what headless tests use.
      - ``"run_async"`` -> ``await circuit0.run_async()``: the browser's free-running loop,
                          which drives the clock and live inputs — this is how a clocked
                          circuit actually steps interactively.
      - ``"test"``      -> ``<test>.evaluate(circuit0)`` for each Test component. evaluate()
                          settles (or, for a clocked Test, pulses reset then cycles the clock
                          until the stop output is reached) and checks the expected outputs —
                          a bounded, self-checking run, so it is how a *sequential* circuit is
                          exercised headlessly (grade.py, pytest, plain exec — synchronous).
      - ``"test_async"``-> ``await <test>.evaluate_async(circuit0)`` for each Test. The browser
                          path: same checks as "test", but the cooperative variant yields to the
                          event loop between clock cycles so a long clocked run keeps the UI
                          responsive, renders live updates, and can be stopped.
    """
    subdefs = ggc.get("schematicComponents", {}) or {}
    lines = [
        "from ggl import arithmetic, circuit, component, io, logic, memory, plexers, wires"
    ]
    # Tunnels join by label through a process-global registry; clear it so a prior run's
    # tunnels (in a persistent Pyodide/CPython session) never leak into this circuit.
    lines.append("wires.Tunnel.reset_history()")

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
        name = subdefs[circuit_id].get('definition', {}).get('name', circuit_id)
        lines.append("")
        lines.append(f"# subcircuit: {name}")
        lines.append(f'{cvar} = circuit.Circuit(circuit_name="{_esc(name)}")')
        _emit_body(inner.get("components", []) or [], inner.get("wires", []) or [],
                   inner.get("wireJunctions", []) or [],
                   cvar, subdefs, templates, lines, is_top=False, circuit_name=name)
        tvar = f"Tmpl_{_san(circuit_id)}"
        lines.append(f"{tvar} = circuit.Component({cvar})")
        templates[circuit_id] = tvar
        return tvar

    for comp in ggc.get("components", []) or []:
        if comp.get("type") == "schematic-component":
            ensure_template((comp.get("props", {}) or {}).get("circuitId"))

    lines.append("")
    top_name = ggc.get("name")
    lines.append(
        f'circuit0 = circuit.Circuit(circuit_name="{_esc(top_name)}")'
        if top_name
        else "circuit0 = circuit.Circuit()"
    )
    _emit_body(ggc.get("components", []) or [], ggc.get("wires", []) or [],
               ggc.get("wireJunctions", []) or [],
               "circuit0", subdefs, templates, lines, is_top=True,
               circuit_name=ggc.get("name"))

    if mode in ("test", "test_async"):
        # Each Test component was declared above (it has no ports, so it took no part in
        # connection resolution); evaluate() runs and checks it against the built circuit.
        # 'test' is the synchronous headless path (grade.py, pytest, plain exec). 'test_async'
        # is the browser path: await the cooperative variant so a long clocked Test yields to
        # the event loop (responsive UI, live updates, working Stop) instead of freezing it.
        call = "await {v}.evaluate_async(circuit0)" if mode == "test_async" else "{v}.evaluate(circuit0)"
        for comp in ggc.get("components", []) or []:
            if comp.get("type") == "test":
                lines.append(call.format(v=_var(comp["id"])))
    elif mode == "run_async":
        lines.append("await circuit0.run_async()")
    else:
        lines.append("circuit0.run()")
    return "\n".join(lines) + "\n"
