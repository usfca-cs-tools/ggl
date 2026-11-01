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

import abc
import re

from .errors import (
    CircuitError,
    ERROR_NET_MULTIPLE_DRIVERS,
    ERROR_TUNNEL_MULTIPLE_DRIVERS,
    ERROR_TUNNEL_NO_DRIVER,
    ERROR_CHIPSROM_NO_PROGRAM,
    ERROR_CHIPSROM_OVERFLOW,
)

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


class GgcPort:
    """A connection point on a component, positioned relative to that component"""

    def __init__(self, data: dict):
        self.name = data.get("name")
        self.x = data.get("x", 0)
        self.y = data.get("y", 0)
        self.direction = data.get("direction")


class GgcComponent:
    """
    One component from a .ggc file. The JSON is parsed here, once, so the rest of this
    module works with declared members. Properties specific to one component type are
    read through prop().
    """

    def __init__(self, data: dict):
        props = data.get("props", {}) or {}
        self.id = data.get("id", "")
        self.type: str = data.get("type", "")
        self.x = data.get("x", 0)
        self.y = data.get("y", 0)
        self.ports = [GgcPort(port) for port in data.get("ports", []) or []]
        self.label = props.get("label", "")
        self.bits = props.get("bits", 1)
        self.circuit_id = props.get("circuitId")
        # A tunnel's direction comes from its wiring, not its stored property. It is set by
        # _assign_tunnel_directions(), and stays None for every other kind of component.
        self.tunnel_direction: str | None = None
        self._props = props

    def prop(self, name, default=None):
        return self._props.get(name, default)

    def is_tunnel(self):
        return self.type == "tunnel"

    def is_subcircuit(self):
        return self.type == "schematic-component"

    def port_position(self, port):
        """The port's absolute coordinate, rounded for matching against wire endpoints"""
        return (_r(self.x + port.x), _r(self.y + port.y))

    def port_direction(self, port):
        if self.tunnel_direction is not None:
            return self.tunnel_direction
        return port.direction


def _parse_components(raw_components):
    return [GgcComponent(data) for data in raw_components or []]


class ComponentEmitter(abc.ABC):
    """
    Emits the GGL expression which constructs one component's Node. The way a Gate
    subclass supplies only logic(), a subclass here supplies only its GGL class and the
    arguments which differ. Every Node takes a label first and a js_id last.
    """

    ggc_type = ""
    ggl_class = ""

    def __init__(self, comp: GgcComponent):
        self.comp = comp

    @abc.abstractmethod
    def arguments(self):
        """The constructor arguments between label and js_id"""

    def expression(self):
        args = [f'label="{_esc(self.comp.label)}"']
        args += self.arguments()
        args.append(f'js_id="{_esc(self.comp.id)}"')
        return f'{self.ggl_class}({", ".join(args)})'

    def initial_value(self, is_top):
        """The int to assign to the Node's .value after construction, or None"""
        return None


class BitsEmitter(ComponentEmitter):
    """A component whose only argument is its bit width"""

    def arguments(self):
        return [f"bits={self.comp.bits}"]


class InputEmitter(BitsEmitter):
    ggc_type = "input"
    ggl_class = "io.Input"

    def stored_value(self):
        return int(self.comp.prop("value", 0) or 0)

    def initial_value(self, is_top):
        # A subcircuit's own Inputs are interface ports driven by the parent
        if is_top:
            return self.stored_value()
        return None


class ConstantEmitter(InputEmitter):
    ggc_type = "constant"
    ggl_class = "io.Constant"

    def initial_value(self, is_top):
        return self.stored_value()


class OutputEmitter(BitsEmitter):
    ggc_type = "output"
    ggl_class = "io.Output"


class ProbeEmitter(BitsEmitter):
    """A Probe is a value observer, not an interface port (see io.Probe)"""
    ggc_type = "probe"
    ggl_class = "io.Probe"


class ClockEmitter(ComponentEmitter):
    ggc_type = "clock"
    ggl_class = "io.Clock"

    def arguments(self):
        frequency = self.comp.prop("frequency", 1)
        mode = _esc(self.comp.prop("mode", "auto"))
        return [f"frequency={frequency}", f'mode="{mode}"']


class GateEmitter(ComponentEmitter):
    """Every gate omits the arguments which are at their engine defaults"""

    def arguments(self):
        args = []
        if self.comp.bits != 1:
            args.append(f"bits={self.comp.bits}")
        num_inputs = self.comp.prop("numInputs")
        if num_inputs is not None:
            args.append(f"num_inputs={int(num_inputs)}")
        inverted_inputs = self.comp.prop("invertedInputs")
        if inverted_inputs:
            args.append(f"inverted_inputs={list(inverted_inputs)}")
        return args


class AndEmitter(GateEmitter):
    ggc_type = "and-gate"
    ggl_class = "logic.And"


class OrEmitter(GateEmitter):
    ggc_type = "or-gate"
    ggl_class = "logic.Or"


class NotEmitter(GateEmitter):
    ggc_type = "not-gate"
    ggl_class = "logic.Not"


class NandEmitter(GateEmitter):
    ggc_type = "nand-gate"
    ggl_class = "logic.Nand"


class NorEmitter(GateEmitter):
    ggc_type = "nor-gate"
    ggl_class = "logic.Nor"


class XorEmitter(GateEmitter):
    ggc_type = "xor-gate"
    ggl_class = "logic.Xor"


class XnorEmitter(GateEmitter):
    ggc_type = "xnor-gate"
    ggl_class = "logic.Xnor"


class RangesEmitter(ComponentEmitter):
    """A component which splits or merges a bus into (start, end) bit ranges"""

    def ranges(self):
        ranges = []
        for bit_range in self.comp.prop("ranges") or []:
            ranges.append((bit_range.get("start"), bit_range.get("end")))
        return ranges


class SplitterEmitter(RangesEmitter):
    ggc_type = "splitter"
    ggl_class = "wires.Splitter"

    def arguments(self):
        return [f'bits={self.comp.prop("inputBits", 1)}', f"splits={self.ranges()}"]


class MergerEmitter(RangesEmitter):
    ggc_type = "merger"
    ggl_class = "wires.Merger"

    def arguments(self):
        return [f'bits={self.comp.prop("outputBits", 1)}', f"merge_inputs={self.ranges()}"]


class TunnelEmitter(ComponentEmitter):
    """Same-label tunnels are joined inside the engine"""
    ggc_type = "tunnel"
    ggl_class = "wires.Tunnel"

    def arguments(self):
        direction = self.comp.tunnel_direction
        if direction is None:
            direction = self.comp.prop("direction", "input")
        return [f'direction="{_esc(direction)}"']


class AdderEmitter(BitsEmitter):
    ggc_type = "adder"
    ggl_class = "arithmetic.Adder"


class SubtractEmitter(BitsEmitter):
    ggc_type = "subtract"
    ggl_class = "arithmetic.Subtract"


class MultiplyEmitter(BitsEmitter):
    ggc_type = "multiply"
    ggl_class = "arithmetic.Multiply"


class DivideEmitter(BitsEmitter):
    ggc_type = "divide"
    ggl_class = "arithmetic.Division"


class CompareEmitter(BitsEmitter):
    ggc_type = "compare"
    ggl_class = "arithmetic.Comparator"


class ShiftEmitter(ComponentEmitter):
    """BarrelShifter wants direction and mode separately; the .ggc packs them into one
    field like "logical_left" or "arithmetic_right"."""
    ggc_type = "shift"
    ggl_class = "arithmetic.BarrelShifter"

    def arguments(self):
        packed_mode = str(self.comp.prop("mode", "logical_left"))
        direction = "left"
        if "right" in packed_mode:
            direction = "right"
        mode = "logical"
        if "arithmetic" in packed_mode:
            mode = "arithmetic"
        return [f"bits={self.comp.bits}", f'direction="{direction}"', f'mode="{mode}"']


class SignExtendEmitter(ComponentEmitter):
    """Has distinct input and output widths, not the shared bits"""
    ggc_type = "signExtend"
    ggl_class = "arithmetic.SignExtend"

    def arguments(self):
        in_bits = int(self.comp.prop("inBits", 1))
        out_bits = int(self.comp.prop("outBits", 1))
        return [f"in_bits={in_bits}", f"out_bits={out_bits}"]


class SelectorEmitter(ComponentEmitter):
    """A plexer sized by its number of selector bits"""

    def arguments(self):
        return [f'selector_bits={int(self.comp.prop("selectorBits", 2))}']


class MultiplexerEmitter(SelectorEmitter):
    ggc_type = "multiplexer"
    ggl_class = "plexers.Multiplexer"

    def arguments(self):
        args = super().arguments()
        if self.comp.bits != 1:
            args.append(f"bits={self.comp.bits}")
        return args


class DecoderEmitter(SelectorEmitter):
    ggc_type = "decoder"
    ggl_class = "plexers.Decoder"


class PriorityEncoderEmitter(SelectorEmitter):
    ggc_type = "priorityEncoder"
    ggl_class = "plexers.PriorityEncoder"


class RegisterEmitter(BitsEmitter):
    ggc_type = "register"
    ggl_class = "memory.Register"


class ChipsRomEmitter(ComponentEmitter):
    """A ROM of 32-bit instruction words, which must hold a program that fits"""
    ggc_type = "chipsROM"
    ggl_class = "memory.ROM"

    def check_program(self, words, capacity):
        comp = self.comp
        if not words:
            raise CircuitError(
                component_id=comp.id, component_type=comp.type, component_label=comp.label,
                error_code=ERROR_CHIPSROM_NO_PROGRAM, label=comp.label)
        if len(words) > capacity:
            raise CircuitError(
                component_id=comp.id, component_type=comp.type, component_label=comp.label,
                error_code=ERROR_CHIPSROM_OVERFLOW, label=comp.label, words=len(words),
                addressBits=self.address_bits(), capacity=capacity)

    def address_bits(self):
        return int(self.comp.prop("addressBits", 6))

    def arguments(self):
        address_bits = self.address_bits()
        capacity = 1 << address_bits
        words = self.comp.prop("words") or []
        self.check_program(words, capacity)
        data = [0] * capacity
        for i, word in enumerate(words):
            data[i] = int(word)
        return [f"address_bits={address_bits}", "data_bits=32", f"data={data}"]


class RomEmitter(ComponentEmitter):
    """Pads the data to the address space, and clamps each value to the data width"""
    ggc_type = "rom"
    ggl_class = "memory.ROM"

    def arguments(self):
        address_bits = int(self.comp.prop("addressBits", 4))
        data_bits = int(self.comp.prop("dataBits", 8))
        capacity = 1 << address_bits
        max_value = (1 << data_bits) - 1
        stored = self.comp.prop("data") or []
        data = [0] * capacity
        for i, value in enumerate(stored[:capacity]):
            data[i] = max(0, min(int(value), max_value))
        return [f"address_bits={address_bits}", f"data_bits={data_bits}", f"data={data}"]


class RamEmitter(ComponentEmitter):
    ggc_type = "ram"
    ggl_class = "memory.RAM"

    def arguments(self):
        address_bits = int(self.comp.prop("addressBits", 4))
        data_bits = int(self.comp.prop("dataBits", 8))
        return [f"address_bits={address_bits}", f"data_bits={data_bits}"]


class TestEmitter(ComponentEmitter):
    """A verification directive, not a circuit node. It has no ports."""
    ggc_type = "test"
    ggl_class = "io.Test"

    def table_arguments(self):
        table = self.comp.prop("table") or {}
        return [
            f'input_names={_str_list(table.get("inputNames") or [])}',
            f'output_names={_str_list(table.get("outputNames") or [])}',
            f'rows={_row_list(table.get("rows") or [])}',
        ]

    def stop_arguments(self):
        if not self.comp.prop("stop_enabled"):
            return []
        return [
            "stop_enabled=True",
            f'stop_output_name="{_esc(self.comp.prop("stop_output_name", ""))}"',
            f'stop_output_value={int(self.comp.prop("stop_output_value") or 0)}',
            # Cap on how long a stuck clocked test waits before failing. Default 1000
            # (matches io.Test) so an unreached stop condition surfaces quickly.
            f'max_cycles={int(self.comp.prop("max_cycles") or 1000)}',
        ]

    def reset_arguments(self):
        if not self.comp.prop("reset_enabled"):
            return []
        return [
            "reset_enabled=True",
            f'reset_input_name="{_esc(self.comp.prop("reset_input_name", ""))}"',
        ]

    def arguments(self):
        return self.table_arguments() + self.stop_arguments() + self.reset_arguments()


_EMITTERS = {emitter.ggc_type: emitter for emitter in (
    InputEmitter, ConstantEmitter, OutputEmitter, ProbeEmitter, ClockEmitter,
    AndEmitter, OrEmitter, NotEmitter, NandEmitter, NorEmitter, XorEmitter, XnorEmitter,
    SplitterEmitter, MergerEmitter, TunnelEmitter,
    AdderEmitter, SubtractEmitter, MultiplyEmitter, DivideEmitter, CompareEmitter,
    ShiftEmitter, SignExtendEmitter,
    MultiplexerEmitter, DecoderEmitter, PriorityEncoderEmitter,
    RegisterEmitter, ChipsRomEmitter, RomEmitter, RamEmitter, TestEmitter,
)}


def _emitter_for(comp: GgcComponent):
    """The emitter for a component, or None if its type isn't a runnable node"""
    emitter = _EMITTERS.get(comp.type)
    if emitter is None:
        return None
    return emitter(comp)


def _component_expr(data, tunnel_dir=None):
    """Return (construction_expr, value_init) for one component's JSON, or (None, None) if
    its type isn't a runnable node. Used by tests."""
    comp = GgcComponent(data)
    comp.tunnel_direction = tunnel_dir
    emitter = _emitter_for(comp)
    if emitter is None:
        return (None, None)
    return (emitter.expression(), emitter.initial_value(is_top=True))


def _inner_labels(subdefs, circuit_id, direction):
    """Ordered inner Input/Output labels of a subcircuit — the port names its
    CircuitNode exposes."""
    inner = subdefs.get(circuit_id, {}).get("circuit", {}) or {}
    if direction == "input":
        wanted_type = "input"
    else:
        wanted_type = "output"
    labels = []
    for comp in _parse_components(inner.get("components")):
        if comp.type == wanted_type:
            labels.append(comp.label)
    return labels


def _external_port_name(comp, port, direction, subdefs):
    """A subcircuit placement names its ports by position ("0", "1", ...), but its
    CircuitNode names them by the inner circuit's labels, so translate. Every other
    component's port name is already correct."""
    if not comp.is_subcircuit() or comp.circuit_id not in subdefs:
        return port.name
    labels = _inner_labels(subdefs, comp.circuit_id, direction)
    try:
        return labels[int(port.name)]
    except (ValueError, TypeError, IndexError):
        return port.name


def _port_index(components, var_of, subdefs):
    """Map rounded absolute coordinate (x, y) -> list of (var, port_name, direction)."""
    index: dict = {}
    for comp in components:
        var = var_of.get(comp.id)
        if var is None:
            continue
        for port in comp.ports:
            direction = comp.port_direction(port)
            name = _external_port_name(comp, port, direction, subdefs)
            index.setdefault(comp.port_position(port), []).append((var, name, direction))
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


def _assign_tunnel_directions(components, wires, junctions):
    """Infer each wired tunnel's direction from what it's wired to, ignoring the stored prop.

    A tunnel whose local net contains a real driver (a non-tunnel output port) is a
    publisher: it reads that driven value and pushes it onto the named net, so its own port
    is an input. Otherwise the tunnel is a subscriber — it sources the net's value onto a
    local wire, so its port is an output. An unwired tunnel is in no net and stays None.
    """
    from collections import defaultdict

    comps_at = defaultdict(list)  # coord -> [(component, stored port direction)]
    for comp in components:
        for port in comp.ports:
            comps_at[comp.port_position(port)].append((comp, port.direction))

    for net in _group_nets(wires, junctions):
        coords = {key for i in net for key in _endpoints(wires[i])}
        has_driver = False
        for key in coords:
            for comp, direction in comps_at.get(key, []):
                if direction == "output" and not comp.is_tunnel():
                    has_driver = True
        for key in coords:
            for comp, _direction in comps_at.get(key, []):
                if comp.is_tunnel():
                    if has_driver:
                        comp.tunnel_direction = "input"
                    else:
                        comp.tunnel_direction = "output"


def _validate_tunnel_nets(components):
    """Raise a CircuitError for a tunnel net that cannot work: no driver (every same-label
    tunnel is a subscriber, so they would read nothing) or more than one driver (two
    publishers contend). Only labelled, wired tunnels form a net."""
    from collections import defaultdict

    publishers_of = defaultdict(list)
    subscribers_of = defaultdict(list)
    labels = []
    for comp in components:
        if not comp.is_tunnel() or comp.tunnel_direction is None or not comp.label:
            continue
        if comp.label not in labels:
            labels.append(comp.label)
        if comp.tunnel_direction == "input":
            publishers_of[comp.label].append(comp)
        else:
            subscribers_of[comp.label].append(comp)

    for label in labels:
        publishers = publishers_of[label]
        subscribers = subscribers_of[label]
        if len(publishers) >= 2:
            raise CircuitError(
                component_id=publishers[1].id, component_type="tunnel",
                component_label=label, error_code=ERROR_TUNNEL_MULTIPLE_DRIVERS, label=label)
        if not publishers and subscribers:
            raise CircuitError(
                component_id=subscribers[0].id, component_type="tunnel",
                component_label=label, error_code=ERROR_TUNNEL_NO_DRIVER, label=label)


def _driver_key(var, port, comp):
    """Same-label subscriber tunnels on one net carry the same value, so they share a key
    and count as one driver. Any other output port is its own driver."""
    if comp.is_tunnel():
        return ("tunnel", comp.label)
    return (var, port)


def _describe_driver(comp, port):
    """A driver's name for an error message. A subcircuit's positional port is shown by its
    inner label (as resolved in the index)."""
    name = comp.label or comp.type
    output_count = sum(1 for p in comp.ports if p.direction == "output")
    if output_count > 1:
        return f"{name}.{port}"
    return name


def _check_single_driver(outs, comp_of_var, circuit_name):
    """Raise a CircuitError if a net has more than one driver (output port). Two outputs
    wired together can't both set the wire's value, so the circuit can't be right — e.g. a
    stray wire joining two of a subcircuit's output pins."""
    drivers = []
    seen = set()
    for var, port in outs:
        comp = comp_of_var[var]
        key = _driver_key(var, port, comp)
        if key not in seen:
            seen.add(key)
            drivers.append((comp, port))
    if len(drivers) < 2:
        return

    first, first_port = drivers[0]
    second = drivers[1][0]
    raise CircuitError(
        component_id=first.id, component_type=first.type, component_label=first.label,
        error_code=ERROR_NET_MULTIPLE_DRIVERS, port_name=first_port,
        connected_component_id=second.id, circuit_name=circuit_name,
        count=len(drivers), drivers=", ".join(_describe_driver(c, p) for c, p in drivers))


def _resolve_connections(wires, junctions, index, comp_of_var=None, circuit_name=None):
    """Group wires into electrical nets and return (src, dst, wire_id) connections.

    A net is a maximal set of wires joined either by a shared endpoint coordinate or by
    a junction (a branch wire meeting another wire mid-run). Each net's ports are found
    by matching every endpoint against the port index; the net's one output drives each
    of its inputs. This subsumes both fan-out styles: multiple wires off one output port,
    and junction branches off a trunk wire. With ``comp_of_var`` (var -> component dict),
    a net with more than one driver raises a CircuitError rather than silently using the
    first.
    """
    conns = []
    for net in _group_nets(wires, junctions):
        outs: list = []
        ins: list = []
        input_wire: dict = {}
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
        if comp_of_var is not None and len(outs) > 1:
            _check_single_driver(outs, comp_of_var, circuit_name)
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
        if comp.type not in ("input", "output"):
            continue
        var = var_of.get(comp.id)
        if var is None:
            continue
        if comp.type == "input":
            connected = var in driving
        else:
            connected = var in driven
        if not connected:
            label = comp.label or comp.id
            raise CircuitError(
                component_id=comp.id, component_type=comp.type, component_label=label,
                error_code="portNotFullyConnected", circuit_name=circuit_name,
                port_name=label, label=label)


def _emit_body(components, wires, junctions, circ_var, subdefs, templates, lines, is_top,
               circuit_name=None):
    """Emit node declarations and connect() calls for one circuit (top level or a
    subcircuit body) into ``circ_var``."""
    # A tunnel's direction is derived from its wiring (not the stored prop), and same-label
    # tunnels must form a valid net (exactly one driver). Resolve and check both up front.
    _assign_tunnel_directions(components, wires, junctions)
    _validate_tunnel_nets(components)

    var_of = {}
    value_inits = []
    for comp in components:
        if comp.is_subcircuit():
            tvar = templates.get(comp.circuit_id)
            if tvar is None:
                continue  # unknown/undefined subcircuit
            var = _var(comp.id)
            var_of[comp.id] = var
            lines.append(f"{var} = {tvar}()")
            continue
        emitter = _emitter_for(comp)
        if emitter is None:
            continue
        var = _var(comp.id)
        var_of[comp.id] = var
        lines.append(f"{var} = {emitter.expression()}")
        init = emitter.initial_value(is_top)
        if init is not None:
            value_inits.append(f"{var}.value = {init}")
    lines.extend(value_inits)

    index = _port_index(components, var_of, subdefs)
    comp_of_var = {var_of[c.id]: c for c in components if c.id in var_of}
    conns = _resolve_connections(wires, junctions, index, comp_of_var, circuit_name)
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

    templates: dict = {}  # circuitId -> template variable name
    # circuitIds whose templates are being emitted, outermost first. A .ggc names subcircuits
    # by reference, so a circuit can (directly or through others) place itself — something the
    # GGL API can't express, since a Component instance is a snapshot copy. Resolving the
    # references is where such a cycle exists, so it's caught here rather than recursing forever.
    in_progress: list = []

    def _name_of(circuit_id):
        return subdefs[circuit_id].get('definition', {}).get('name', circuit_id)

    def ensure_template(circuit_id, placement):
        if circuit_id in templates:
            return templates[circuit_id]
        if circuit_id not in subdefs:
            return None
        if circuit_id in in_progress:
            cycle = in_progress[in_progress.index(circuit_id):] + [circuit_id]
            raise CircuitError(
                component_id=placement.id, component_type="schematic-component",
                component_label=_name_of(circuit_id), error_code="recursiveSubcircuit",
                circuit_name=_name_of(in_progress[-1]),
                chain=" → ".join(_name_of(c) for c in cycle))
        in_progress.append(circuit_id)
        inner = subdefs[circuit_id].get("circuit", {}) or {}
        inner_components = _parse_components(inner.get("components"))
        for child in inner_components:
            if child.is_subcircuit():
                ensure_template(child.circuit_id, child)
        in_progress.pop()
        cvar = f"sub_{_san(circuit_id)}"
        name = _name_of(circuit_id)
        lines.append("")
        lines.append(f"# subcircuit: {name}")
        lines.append(f'{cvar} = circuit.Circuit(circuit_name="{_esc(name)}")')
        _emit_body(inner_components, inner.get("wires", []) or [],
                   inner.get("wireJunctions", []) or [],
                   cvar, subdefs, templates, lines, is_top=False, circuit_name=name)
        tvar = f"Tmpl_{_san(circuit_id)}"
        lines.append(f"{tvar} = circuit.Component({cvar})")
        templates[circuit_id] = tvar
        return tvar

    top_components = _parse_components(ggc.get("components"))
    for comp in top_components:
        if comp.is_subcircuit():
            ensure_template(comp.circuit_id, comp)

    lines.append("")
    top_name = ggc.get("name")
    lines.append(
        f'circuit0 = circuit.Circuit(circuit_name="{_esc(top_name)}")'
        if top_name
        else "circuit0 = circuit.Circuit()"
    )
    _emit_body(top_components, ggc.get("wires", []) or [],
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
        for comp in top_components:
            if comp.type == "test":
                lines.append(call.format(v=_var(comp.id)))
    elif mode == "run_async":
        lines.append("await circuit0.run_async()")
    else:
        lines.append("circuit0.run()")
    return "\n".join(lines) + "\n"
