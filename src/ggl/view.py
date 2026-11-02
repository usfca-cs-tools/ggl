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


def _position(pos):
    return (_r(pos.get("x")), _r(pos.get("y")))


def _on_segment(point, start, end):
    """True when point is collinear with the segment and within its bounding box"""
    px, py = point
    ax, ay = start
    bx, by = end
    if abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) >= 1e-6:
        return False
    in_x_range = min(ax, bx) - 1e-6 <= px <= max(ax, bx) + 1e-6
    in_y_range = min(ay, by) - 1e-6 <= py <= max(ay, by) + 1e-6
    return in_x_range and in_y_range


class GgcWire:
    """A wire from a .ggc file: a routed polyline between two endpoints"""

    def __init__(self, data: dict):
        self.id = data.get("id", "")
        start = _position(data.get("startConnection", {}).get("pos", {}))
        end = _position(data.get("endConnection", {}).get("pos", {}))
        self.endpoints = (start, end)
        points = data.get("points")
        if points:
            self.path = [_position(point) for point in points]
        else:
            self.path = [start, end]

    def covers(self, point):
        """True when point lies on the wire, at a vertex or anywhere along a segment"""
        for start, end in zip(self.path, self.path[1:]):
            if _on_segment(point, start, end):
                return True
        return False


class GgcJunction:
    """A T-tap, where a branch wire meets a trunk wire mid-run"""

    def __init__(self, data: dict):
        self.position = _position(data.get("pos") or {})
        self.connected_wire_id = data.get("connectedWireId")


class GgcComponent(abc.ABC):
    """
    One component from a .ggc file. The JSON is parsed here, once, so the rest of this
    module works with declared members. A component also knows the GGL expression which
    constructs its Node. The way a Gate subclass supplies only logic(), a subclass here
    supplies only its GGL class and the arguments which differ. Every Node takes a label
    first and a js_id last.
    """

    ggc_type = ""
    ggl_class = ""

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
        self._props = props

    def prop(self, name, default=None):
        return self._props.get(name, default)

    def is_subcircuit(self):
        return self.type == "schematic-component"

    def is_runnable(self):
        """False for a component which doesn't become a Node in the circuit"""
        return True

    def port_position(self, port):
        """The port's absolute coordinate, rounded for matching against wire endpoints"""
        return (_r(self.x + port.x), _r(self.y + port.y))

    def port_direction(self, port):
        return port.direction

    def drives_net(self, stored_direction):
        """True if a port with this stored direction is a real driver on its net"""
        return stored_direction == "output"

    def driver_key(self, port):
        """Identifies this driver when counting the drivers on a net"""
        return port.identity

    def wired_to_net(self, net_has_driver):
        """Called when this component's port is on a net. Only a tunnel cares."""

    def join_tunnel_net(self, tunnel_nets):
        """Only a tunnel joins a named net"""

    def describe_driver(self, port_name):
        """This driver's name for an error message"""
        name = self.label or self.type
        output_count = sum(1 for port in self.ports if port.direction == "output")
        if output_count > 1:
            return f"{name}.{port_name}"
        return name

    @abc.abstractmethod
    def arguments(self):
        """The constructor arguments between label and js_id"""

    def expression(self):
        args = [f'label="{_esc(self.label)}"']
        args += self.arguments()
        args.append(f'js_id="{_esc(self.id)}"')
        return f'{self.ggl_class}({", ".join(args)})'

    def initial_value(self, is_top):
        """The int to assign to the Node's .value after construction, or None"""
        return None


class GgcUnknown(GgcComponent):
    """A component with no Node, such as a text annotation"""

    def is_runnable(self):
        return False

    def arguments(self):
        return []


class GgcBits(GgcComponent):
    """A component whose only argument is its bit width"""

    def arguments(self):
        return [f"bits={self.bits}"]


class GgcInput(GgcBits):
    ggc_type = "input"
    ggl_class = "io.Input"

    def stored_value(self):
        return int(self.prop("value", 0) or 0)

    def initial_value(self, is_top):
        # A subcircuit's own Inputs are interface ports driven by the parent
        if is_top:
            return self.stored_value()
        return None


class GgcConstant(GgcInput):
    ggc_type = "constant"
    ggl_class = "io.Constant"

    def initial_value(self, is_top):
        return self.stored_value()


class GgcOutput(GgcBits):
    ggc_type = "output"
    ggl_class = "io.Output"


class GgcProbe(GgcBits):
    """A Probe is a value observer, not an interface port (see io.Probe)"""
    ggc_type = "probe"
    ggl_class = "io.Probe"


class GgcClock(GgcComponent):
    ggc_type = "clock"
    ggl_class = "io.Clock"

    def arguments(self):
        frequency = self.prop("frequency", 1)
        mode = _esc(self.prop("mode", "auto"))
        return [f"frequency={frequency}", f'mode="{mode}"']


class GgcGate(GgcComponent):
    """Every gate omits the arguments which are at their engine defaults"""

    def arguments(self):
        args = []
        if self.bits != 1:
            args.append(f"bits={self.bits}")
        num_inputs = self.prop("numInputs")
        if num_inputs is not None:
            args.append(f"num_inputs={int(num_inputs)}")
        inverted_inputs = self.prop("invertedInputs")
        if inverted_inputs:
            args.append(f"inverted_inputs={list(inverted_inputs)}")
        return args


class GgcAnd(GgcGate):
    ggc_type = "and-gate"
    ggl_class = "logic.And"


class GgcOr(GgcGate):
    ggc_type = "or-gate"
    ggl_class = "logic.Or"


class GgcNot(GgcGate):
    ggc_type = "not-gate"
    ggl_class = "logic.Not"


class GgcNand(GgcGate):
    ggc_type = "nand-gate"
    ggl_class = "logic.Nand"


class GgcNor(GgcGate):
    ggc_type = "nor-gate"
    ggl_class = "logic.Nor"


class GgcXor(GgcGate):
    ggc_type = "xor-gate"
    ggl_class = "logic.Xor"


class GgcXnor(GgcGate):
    ggc_type = "xnor-gate"
    ggl_class = "logic.Xnor"


class GgcRanges(GgcComponent):
    """A component which splits or merges a bus into (start, end) bit ranges"""

    def ranges(self):
        ranges = []
        for bit_range in self.prop("ranges") or []:
            ranges.append((bit_range.get("start"), bit_range.get("end")))
        return ranges


class GgcSplitter(GgcRanges):
    ggc_type = "splitter"
    ggl_class = "wires.Splitter"

    def arguments(self):
        return [f'bits={self.prop("inputBits", 1)}', f"splits={self.ranges()}"]


class GgcMerger(GgcRanges):
    ggc_type = "merger"
    ggl_class = "wires.Merger"

    def arguments(self):
        return [f'bits={self.prop("outputBits", 1)}', f"merge_inputs={self.ranges()}"]


class GgcTunnel(GgcComponent):
    """Same-label tunnels are joined inside the engine"""
    ggc_type = "tunnel"
    ggl_class = "wires.Tunnel"

    def __init__(self, data: dict):
        super().__init__(data)
        # Comes from the tunnel's wiring, not its stored property. None until it's wired.
        self.tunnel_direction: str | None = None

    def port_direction(self, port):
        if self.tunnel_direction is not None:
            return self.tunnel_direction
        return port.direction

    def drives_net(self, stored_direction):
        return False

    def driver_key(self, port):
        # Same-label subscribers on one net carry the same value, so they count as one driver
        return ("tunnel", self.label)

    def wired_to_net(self, net_has_driver):
        """A tunnel on a net with a real driver is a publisher: it reads that value and pushes
        it onto the named net, so its own port is an input. Otherwise it's a subscriber: it
        sources the named net's value onto a local wire, so its port is an output."""
        if net_has_driver:
            self.tunnel_direction = "input"
        else:
            self.tunnel_direction = "output"

    def join_tunnel_net(self, tunnel_nets):
        # An unwired or unlabelled tunnel names no net
        if self.tunnel_direction is None or not self.label:
            return
        tunnel_net = tunnel_nets.setdefault(self.label, TunnelNet(self.label))
        tunnel_net.add(self)

    def arguments(self):
        direction = self.tunnel_direction
        if direction is None:
            direction = self.prop("direction", "input")
        return [f'direction="{_esc(direction)}"']


class GgcAdder(GgcBits):
    ggc_type = "adder"
    ggl_class = "arithmetic.Adder"


class GgcSubtract(GgcBits):
    ggc_type = "subtract"
    ggl_class = "arithmetic.Subtract"


class GgcMultiply(GgcBits):
    ggc_type = "multiply"
    ggl_class = "arithmetic.Multiply"


class GgcDivide(GgcBits):
    ggc_type = "divide"
    ggl_class = "arithmetic.Division"


class GgcCompare(GgcBits):
    ggc_type = "compare"
    ggl_class = "arithmetic.Comparator"


class GgcShift(GgcComponent):
    """BarrelShifter wants direction and mode separately; the .ggc packs them into one
    field like "logical_left" or "arithmetic_right"."""
    ggc_type = "shift"
    ggl_class = "arithmetic.BarrelShifter"

    def arguments(self):
        packed_mode = str(self.prop("mode", "logical_left"))
        direction = "left"
        if "right" in packed_mode:
            direction = "right"
        mode = "logical"
        if "arithmetic" in packed_mode:
            mode = "arithmetic"
        return [f"bits={self.bits}", f'direction="{direction}"', f'mode="{mode}"']


class GgcSignExtend(GgcComponent):
    """Has distinct input and output widths, not the shared bits"""
    ggc_type = "signExtend"
    ggl_class = "arithmetic.SignExtend"

    def arguments(self):
        in_bits = int(self.prop("inBits", 1))
        out_bits = int(self.prop("outBits", 1))
        return [f"in_bits={in_bits}", f"out_bits={out_bits}"]


class GgcSelector(GgcComponent):
    """A plexer sized by its number of selector bits"""

    def arguments(self):
        return [f'selector_bits={int(self.prop("selectorBits", 2))}']


class GgcMultiplexer(GgcSelector):
    ggc_type = "multiplexer"
    ggl_class = "plexers.Multiplexer"

    def arguments(self):
        args = super().arguments()
        if self.bits != 1:
            args.append(f"bits={self.bits}")
        return args


class GgcDecoder(GgcSelector):
    ggc_type = "decoder"
    ggl_class = "plexers.Decoder"


class GgcPriorityEncoder(GgcSelector):
    ggc_type = "priorityEncoder"
    ggl_class = "plexers.PriorityEncoder"


class GgcRegister(GgcBits):
    ggc_type = "register"
    ggl_class = "memory.Register"


class GgcChipsRom(GgcComponent):
    """A ROM of 32-bit instruction words, which must hold a program that fits"""
    ggc_type = "chipsROM"
    ggl_class = "memory.ROM"

    def check_program(self, words, capacity):
        if not words:
            raise CircuitError(
                component_id=self.id, component_type=self.type, component_label=self.label,
                error_code=ERROR_CHIPSROM_NO_PROGRAM, label=self.label)
        if len(words) > capacity:
            raise CircuitError(
                component_id=self.id, component_type=self.type, component_label=self.label,
                error_code=ERROR_CHIPSROM_OVERFLOW, label=self.label, words=len(words),
                addressBits=self.address_bits(), capacity=capacity)

    def address_bits(self):
        return int(self.prop("addressBits", 6))

    def arguments(self):
        address_bits = self.address_bits()
        capacity = 1 << address_bits
        words = self.prop("words") or []
        self.check_program(words, capacity)
        data = [0] * capacity
        for i, word in enumerate(words):
            data[i] = int(word)
        return [f"address_bits={address_bits}", "data_bits=32", f"data={data}"]


class GgcRom(GgcComponent):
    """Pads the data to the address space, and clamps each value to the data width"""
    ggc_type = "rom"
    ggl_class = "memory.ROM"

    def arguments(self):
        address_bits = int(self.prop("addressBits", 4))
        data_bits = int(self.prop("dataBits", 8))
        capacity = 1 << address_bits
        max_value = (1 << data_bits) - 1
        stored = self.prop("data") or []
        data = [0] * capacity
        for i, value in enumerate(stored[:capacity]):
            data[i] = max(0, min(int(value), max_value))
        return [f"address_bits={address_bits}", f"data_bits={data_bits}", f"data={data}"]


class GgcRam(GgcComponent):
    ggc_type = "ram"
    ggl_class = "memory.RAM"

    def arguments(self):
        address_bits = int(self.prop("addressBits", 4))
        data_bits = int(self.prop("dataBits", 8))
        return [f"address_bits={address_bits}", f"data_bits={data_bits}"]


class GgcTest(GgcComponent):
    """A verification directive, not a circuit node. It has no ports."""
    ggc_type = "test"
    ggl_class = "io.Test"

    def table_arguments(self):
        table = self.prop("table") or {}
        return [
            f'input_names={_str_list(table.get("inputNames") or [])}',
            f'output_names={_str_list(table.get("outputNames") or [])}',
            f'rows={_row_list(table.get("rows") or [])}',
        ]

    def stop_arguments(self):
        if not self.prop("stop_enabled"):
            return []
        return [
            "stop_enabled=True",
            f'stop_output_name="{_esc(self.prop("stop_output_name", ""))}"',
            f'stop_output_value={int(self.prop("stop_output_value") or 0)}',
            # Cap on how long a stuck clocked test waits before failing. Default 1000
            # (matches io.Test) so an unreached stop condition surfaces quickly.
            f'max_cycles={int(self.prop("max_cycles") or 1000)}',
        ]

    def reset_arguments(self):
        if not self.prop("reset_enabled"):
            return []
        return [
            "reset_enabled=True",
            f'reset_input_name="{_esc(self.prop("reset_input_name", ""))}"',
        ]

    def arguments(self):
        return self.table_arguments() + self.stop_arguments() + self.reset_arguments()


_COMPONENT_CLASSES = {cls.ggc_type: cls for cls in (
    GgcInput, GgcConstant, GgcOutput, GgcProbe, GgcClock,
    GgcAnd, GgcOr, GgcNot, GgcNand, GgcNor, GgcXor, GgcXnor,
    GgcSplitter, GgcMerger, GgcTunnel,
    GgcAdder, GgcSubtract, GgcMultiply, GgcDivide, GgcCompare,
    GgcShift, GgcSignExtend,
    GgcMultiplexer, GgcDecoder, GgcPriorityEncoder,
    GgcRegister, GgcChipsRom, GgcRom, GgcRam, GgcTest,
)}


def parse_component(data):
    component_class = _COMPONENT_CLASSES.get(data.get("type"), GgcUnknown)
    return component_class(data)


def _parse_components(raw_components):
    return [parse_component(data) for data in raw_components or []]


def _component_expr(data, tunnel_dir=None):
    """Return (construction_expr, value_init) for one component's JSON, or (None, None) if
    its type isn't a runnable node. Used by tests."""
    comp = parse_component(data)
    if tunnel_dir is not None:
        comp.wired_to_net(tunnel_dir == "input")
    if not comp.is_runnable():
        return (None, None)
    return (comp.expression(), comp.initial_value(is_top=True))


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


class NetPort:
    """A component's port as a net sees it. Identity is (variable, port name)."""

    def __init__(self, comp, variable, name, direction):
        self.comp = comp
        self.variable = variable
        self.name = name
        self.direction = direction
        self.identity = (variable, name)


class PortIndex:
    """Finds the ports at a coordinate, which is how wires are matched to components"""

    def __init__(self):
        self.ports_at_position = {}

    def add(self, position, port):
        self.ports_at_position.setdefault(position, []).append(port)

    def ports_at(self, position):
        return self.ports_at_position.get(position, [])


def _port_index(components, var_of, subdefs):
    index = PortIndex()
    for comp in components:
        var = var_of.get(comp.id)
        if var is None:
            continue
        for port in comp.ports:
            direction = comp.port_direction(port)
            name = _external_port_name(comp, port, direction, subdefs)
            index.add(comp.port_position(port), NetPort(comp, var, name, direction))
    return index


class Connection:
    """One connect() call: a driver, a port it drives, and the wire which carries it"""

    def __init__(self, source, dest, wire_id):
        self.source = source
        self.dest = dest
        self.wire_id = wire_id

    def sort_key(self):
        return (self.source.identity, self.dest.identity)

    def statement(self, circuit_variable):
        source = f'{self.source.variable}.output("{self.source.name}")'
        dest = f'{self.dest.variable}.input("{self.dest.name}")'
        return f'{circuit_variable}.connect({source}, {dest}, js_id="{_esc(self.wire_id)}")'


class Net:
    """
    An electrical net: a maximal set of wires which are joined together. Its one driver
    feeds every sink. This covers both fan-out styles: multiple wires off one output port,
    and junction branches off a trunk wire.
    """

    def __init__(self, wires):
        self.wires = wires
        self.drivers = {}         # identity -> NetPort
        self.sinks = {}           # identity -> NetPort
        self.sink_wire_ids = {}   # identity -> id of the first wire reaching the sink

    def coordinates(self):
        coordinates = set()
        for wire in self.wires:
            coordinates.update(wire.endpoints)
        return coordinates

    def find_ports(self, index):
        for wire in self.wires:
            for position in wire.endpoints:
                for port in index.ports_at(position):
                    self.add_port(port, wire)

    def add_port(self, port, wire):
        if port.direction == "output":
            self.drivers.setdefault(port.identity, port)
        else:
            self.sinks.setdefault(port.identity, port)
            self.sink_wire_ids.setdefault(port.identity, wire.id)

    def check_single_driver(self, circuit_name):
        """Two outputs wired together can't both set the wire's value, so the circuit can't
        be right. For example, a stray wire joining two of a subcircuit's output pins."""
        distinct = {}
        for port in self.drivers.values():
            distinct.setdefault(port.comp.driver_key(port), port)
        if len(distinct) < 2:
            return
        ports = list(distinct.values())
        first = ports[0]
        raise CircuitError(
            component_id=first.comp.id, component_type=first.comp.type,
            component_label=first.comp.label, error_code=ERROR_NET_MULTIPLE_DRIVERS,
            port_name=first.name, connected_component_id=ports[1].comp.id,
            circuit_name=circuit_name, count=len(ports),
            drivers=", ".join(port.comp.describe_driver(port.name) for port in ports))

    def connections(self):
        if not self.drivers:
            return []
        source = next(iter(self.drivers.values()))
        return [Connection(source, sink, self.sink_wire_ids[sink.identity])
                for sink in self.sinks.values()]


class NetBuilder:
    """Groups wires into nets using union-find. Wires are on one net if they share an
    endpoint coordinate or meet at a junction."""

    def __init__(self, wires, junctions):
        self.wires = wires
        self.junctions = junctions
        self.parent = list(range(len(wires)))

    def find(self, i):
        root = i
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[i] != root:
            next_i = self.parent[i]
            self.parent[i] = root
            i = next_i
        return root

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)

    def join_shared_endpoints(self):
        wires_at = {}
        for i, wire in enumerate(self.wires):
            for position in wire.endpoints:
                wires_at.setdefault(position, []).append(i)
        for group in wires_at.values():
            for other in group[1:]:
                self.union(group[0], other)

    def join_junctions(self):
        """Find a junction's wires by geometry: every wire whose path passes through it. Don't
        use the serialized sourceWireIndex. After edits it can point at an unrelated wire and
        weld two independent nets together, silently dropping a driver. connectedWireId is a
        stable id, so include it in case a branch endpoint rounds off the path."""
        index_of = {wire.id: i for i, wire in enumerate(self.wires)}
        for junction in self.junctions:
            touching = [i for i, wire in enumerate(self.wires) if wire.covers(junction.position)]
            connected = index_of.get(junction.connected_wire_id)
            if connected is not None and connected not in touching:
                touching.append(connected)
            for other in touching[1:]:
                self.union(touching[0], other)

    def build(self):
        self.join_shared_endpoints()
        self.join_junctions()
        groups = {}
        for i, wire in enumerate(self.wires):
            groups.setdefault(self.find(i), []).append(wire)
        return [Net(wires) for wires in groups.values()]


class TunnelNet:
    """Same-label tunnels, which must have exactly one publisher to work"""

    def __init__(self, label):
        self.label = label
        self.publishers = []
        self.subscribers = []

    def add(self, tunnel):
        if tunnel.tunnel_direction == "input":
            self.publishers.append(tunnel)
        else:
            self.subscribers.append(tunnel)

    def check(self):
        if len(self.publishers) >= 2:
            self.raise_error(self.publishers[1], ERROR_TUNNEL_MULTIPLE_DRIVERS)
        if not self.publishers and self.subscribers:
            self.raise_error(self.subscribers[0], ERROR_TUNNEL_NO_DRIVER)

    def raise_error(self, tunnel, error_code):
        raise CircuitError(
            component_id=tunnel.id, component_type="tunnel", component_label=self.label,
            error_code=error_code, label=self.label)


class TunnelNets:
    """Infers each wired tunnel's direction from its net, then checks the named nets"""

    def __init__(self, components, nets):
        self.components = components
        self.nets = nets

    def assign_directions(self):
        ports_at = {}  # position -> [(component, stored port direction)]
        for comp in self.components:
            for port in comp.ports:
                ports_at.setdefault(comp.port_position(port), []).append((comp, port.direction))

        for net in self.nets:
            on_net = []
            for position in net.coordinates():
                on_net += ports_at.get(position, [])
            has_driver = any(comp.drives_net(direction) for comp, direction in on_net)
            for comp, _direction in on_net:
                comp.wired_to_net(has_driver)

    def check(self):
        tunnel_nets = {}
        for comp in self.components:
            comp.join_tunnel_net(tunnel_nets)
        for tunnel_net in tunnel_nets.values():
            tunnel_net.check()


def _resolve_connections(nets, index, circuit_name):
    connections = []
    for net in nets:
        net.find_ports(index)
        net.check_single_driver(circuit_name)
        connections += net.connections()
    # Stable output order regardless of union-find internals.
    connections.sort(key=Connection.sort_key)
    return connections


def _validate_interface_connected(components, var_of, conns, circuit_name):
    """A subcircuit's Input/Output nodes are its interface ports. If one isn't wired to
    anything inside the subcircuit it never enters the circuit's inputs/outputs, so the
    Component built from it silently lacks that port — and the *parent's* connect to that
    port then fails deep in the engine with a bare ``KeyError``. Catch it here, at generation
    time, with a clear message naming the circuit and the port. (Only subcircuits: a
    top-level open input/output is a non-fatal 'unused' warning, handled elsewhere.)"""
    driven = {connection.dest.variable for connection in conns}
    driving = {connection.source.variable for connection in conns}
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
    nets = NetBuilder([GgcWire(wire) for wire in wires],
                      [GgcJunction(junction) for junction in junctions]).build()
    tunnel_nets = TunnelNets(components, nets)
    tunnel_nets.assign_directions()
    tunnel_nets.check()

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
        if not comp.is_runnable():
            continue
        var = _var(comp.id)
        var_of[comp.id] = var
        lines.append(f"{var} = {comp.expression()}")
        init = comp.initial_value(is_top)
        if init is not None:
            value_inits.append(f"{var}.value = {init}")
    lines.extend(value_inits)

    index = _port_index(components, var_of, subdefs)
    conns = _resolve_connections(nets, index, circuit_name)
    if not is_top:
        _validate_interface_connected(components, var_of, conns, circuit_name)
    for connection in conns:
        lines.append(connection.statement(circ_var))


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
