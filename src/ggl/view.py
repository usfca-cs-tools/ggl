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


def _sanitize(name):
    """Make a name usable within a Python identifier"""
    return re.sub(r"[^0-9a-zA-Z_]", "_", str(name))


def _str_list(names):
    return "[" + ", ".join(f'"{_esc(n)}"' for n in names) + "]"


def _row_list(rows):
    return "[" + ", ".join(
        "[" + ", ".join(str(int(v or 0)) for v in (row or [])) + "]" for row in rows
    ) + "]"


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
        self.x = data.get("x", 0)
        self.y = data.get("y", 0)
        self.ports = [GgcPort(port) for port in data.get("ports", []) or []]
        self.label = props.get("label", "")
        self.bits = props.get("bits", 1)
        self._props = props

    def prop(self, name, default=None):
        return self._props.get(name, default)

    def variable(self):
        """The name of this component's Node in the generated program"""
        return "n_" + _sanitize(self.id)

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

    def prepare(self, program):
        """Called before this component's circuit is emitted. Only a subcircuit needs to,
        since its template must be emitted first."""

    def external_port_name(self, port, direction, program):
        """The name of this port on the component's Node"""
        return port.name

    def is_interface_port(self, direction):
        """True if this component is one of its circuit's external ports"""
        return False

    def check_interface_connected(self, driving, driven, circuit_name):
        """Only a subcircuit's Inputs and Outputs need to be connected. See raise_not_connected()"""

    def raise_not_connected(self, circuit_name):
        """A subcircuit's Input or Output which isn't wired inside it never enters the
        circuit's inputs or outputs, so the Component built from it silently lacks that port.
        The parent's connect() to that port would then fail deep in the engine with a bare
        KeyError. Catch it at generation time with a message naming the circuit and port."""
        label = self.label or self.id
        raise CircuitError(
            component_id=self.id, component_type=self.ggc_type, component_label=label,
            error_code="portNotFullyConnected", circuit_name=circuit_name,
            port_name=label, label=label)

    def evaluation_statement(self, is_async):
        """The statement which checks this component against the circuit, or None"""
        return None

    def describe_driver(self, port_name):
        """This driver's name for an error message"""
        name = self.label or self.ggc_type
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


class GgcSource(GgcBits):
    """A component which holds a value and pushes it downstream"""

    def stored_value(self):
        return int(self.prop("value", 0) or 0)


class GgcInput(GgcSource):
    ggc_type = "input"
    ggl_class = "io.Input"

    def initial_value(self, is_top):
        # A subcircuit's own Inputs are interface ports driven by the parent
        if is_top:
            return self.stored_value()
        return None

    def is_interface_port(self, direction):
        return direction == "input"

    def check_interface_connected(self, driving, driven, circuit_name):
        if self.variable() not in driving:
            self.raise_not_connected(circuit_name)


class GgcConstant(GgcSource):
    ggc_type = "constant"
    ggl_class = "io.Constant"

    def initial_value(self, is_top):
        return self.stored_value()


class GgcOutput(GgcBits):
    ggc_type = "output"
    ggl_class = "io.Output"

    def is_interface_port(self, direction):
        return direction != "input"

    def check_interface_connected(self, driving, driven, circuit_name):
        if self.variable() not in driven:
            self.raise_not_connected(circuit_name)


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
                component_id=self.id, component_type=self.ggc_type, component_label=self.label,
                error_code=ERROR_CHIPSROM_NO_PROGRAM, label=self.label)
        if len(words) > capacity:
            raise CircuitError(
                component_id=self.id, component_type=self.ggc_type, component_label=self.label,
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

    def evaluation_statement(self, is_async):
        # The async variant yields to the event loop between clock cycles, so a long clocked
        # run in the browser keeps the UI responsive, renders live updates, and can be stopped
        if is_async:
            return f"await {self.variable()}.evaluate_async(circuit0)"
        return f"{self.variable()}.evaluate(circuit0)"


class GgcSubcircuit(GgcComponent):
    """A placement of a saved subcircuit, whose template is emitted once and instantiated here"""
    ggc_type = "schematic-component"

    def __init__(self, data: dict):
        super().__init__(data)
        self.circuit_id = self.prop("circuitId")
        self.template_variable = None

    def is_runnable(self):
        # A reference to an unknown subcircuit has no template, and is skipped
        return self.template_variable is not None

    def arguments(self):
        return []

    def expression(self):
        return f"{self.template_variable}()"

    def prepare(self, program):
        self.template_variable = program.ensure_template(self.circuit_id, self)

    def external_port_name(self, port, direction, program):
        """A placement names its ports by position ("0", "1", ...), but its CircuitNode names
        them by the inner circuit's labels"""
        if not program.has_definition(self.circuit_id):
            return port.name
        labels = program.definition(self.circuit_id).interface_labels(direction)
        try:
            return labels[int(port.name)]
        except (ValueError, TypeError, IndexError):
            return port.name


_COMPONENT_CLASSES = {cls.ggc_type: cls for cls in (
    GgcInput, GgcConstant, GgcOutput, GgcProbe, GgcClock,
    GgcAnd, GgcOr, GgcNot, GgcNand, GgcNor, GgcXor, GgcXnor,
    GgcSplitter, GgcMerger, GgcTunnel,
    GgcAdder, GgcSubtract, GgcMultiply, GgcDivide, GgcCompare,
    GgcShift, GgcSignExtend,
    GgcMultiplexer, GgcDecoder, GgcPriorityEncoder,
    GgcRegister, GgcChipsRom, GgcRom, GgcRam, GgcTest, GgcSubcircuit,
)}


def parse_component(data):
    component_class = _COMPONENT_CLASSES.get(data.get("type"), GgcUnknown)
    return component_class(data)


def _parse_components(raw_components):
    return [parse_component(data) for data in raw_components or []]


class NetPort:
    """A component's port as a net sees it. Identity is (variable, port name)."""

    def __init__(self, comp, name, direction):
        self.comp = comp
        self.variable = comp.variable()
        self.name = name
        self.direction = direction
        self.identity = (self.variable, name)


class PortIndex:
    """Finds the ports at a coordinate, which is how wires are matched to components"""

    def __init__(self):
        self.ports_at_position = {}

    def add(self, position, port):
        self.ports_at_position.setdefault(position, []).append(port)

    def ports_at(self, position):
        return self.ports_at_position.get(position, [])


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
            component_id=first.comp.id, component_type=first.comp.ggc_type,
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


class GgcCircuit:
    """One circuit: the top level of a .ggc file, or a saved subcircuit"""

    def __init__(self, name, data: dict):
        self.name = name
        self.components = _parse_components(data.get("components"))
        self.wires = [GgcWire(wire) for wire in data.get("wires", []) or []]
        self.junctions = [GgcJunction(junction) for junction in data.get("wireJunctions", []) or []]

    def interface_labels(self, direction):
        """The ordered labels of this circuit's Inputs or Outputs, which are the port names
        its CircuitNode exposes"""
        return [comp.label for comp in self.components if comp.is_interface_port(direction)]


class CircuitEmitter:
    """Emits the node declarations and connect() calls for one circuit"""

    def __init__(self, ggc_circuit, circuit_variable, is_top, program):
        self.circuit = ggc_circuit
        self.circuit_variable = circuit_variable
        self.is_top = is_top
        self.program = program

    def emit(self):
        nets = NetBuilder(self.circuit.wires, self.circuit.junctions).build()
        # A tunnel's direction comes from its wiring, and same-label tunnels must form a valid
        # net. Both have to be settled before any ports are resolved.
        tunnel_nets = TunnelNets(self.circuit.components, nets)
        tunnel_nets.assign_directions()
        tunnel_nets.check()

        self.declare_nodes()
        connections = self.resolve_connections(nets)
        # At the top level, an open input or output is only a warning, handled elsewhere
        if not self.is_top:
            self.check_interface_connected(connections)
        for connection in connections:
            self.program.lines.append(connection.statement(self.circuit_variable))

    def declare_nodes(self):
        value_statements = []
        for comp in self.circuit.components:
            if not comp.is_runnable():
                continue
            self.program.lines.append(f"{comp.variable()} = {comp.expression()}")
            value = comp.initial_value(self.is_top)
            if value is not None:
                value_statements.append(f"{comp.variable()}.value = {value}")
        self.program.lines.extend(value_statements)

    def port_index(self):
        index = PortIndex()
        for comp in self.circuit.components:
            if not comp.is_runnable():
                continue
            for port in comp.ports:
                direction = comp.port_direction(port)
                name = comp.external_port_name(port, direction, self.program)
                index.add(comp.port_position(port), NetPort(comp, name, direction))
        return index

    def resolve_connections(self, nets):
        index = self.port_index()
        connections = []
        for net in nets:
            net.find_ports(index)
            net.check_single_driver(self.circuit.name)
            connections += net.connections()
        # Stable output order regardless of union-find internals
        connections.sort(key=Connection.sort_key)
        return connections

    def check_interface_connected(self, connections):
        driven = {connection.dest.variable for connection in connections}
        driving = {connection.source.variable for connection in connections}
        for comp in self.circuit.components:
            if comp.is_runnable():
                comp.check_interface_connected(driving, driven, self.circuit.name)


class ProgramGenerator:
    """Generates a whole GGL program: a template for each subcircuit used, deepest first, then
    the top-level circuit, then the statements which run it"""

    def __init__(self, ggc, mode):
        self.mode = mode
        self.top = GgcCircuit(ggc.get("name"), ggc)
        self.subcircuit_data = ggc.get("schematicComponents", {}) or {}
        self.definitions = {}  # circuitId -> GgcCircuit, parsed when first used
        self.templates = {}    # circuitId -> template variable name
        # circuitIds whose templates are being emitted, outermost first. A .ggc names
        # subcircuits by reference, so a circuit can place itself, directly or through others.
        # The GGL API can't express that, so it's caught here rather than recursing forever.
        self.in_progress = []
        self.lines = ["from ggl import arithmetic, circuit, component, io, logic, memory, plexers, wires"]

    def has_definition(self, circuit_id):
        return circuit_id in self.subcircuit_data

    def name_of(self, circuit_id):
        return self.subcircuit_data[circuit_id].get("definition", {}).get("name", circuit_id)

    def definition(self, circuit_id):
        if circuit_id not in self.definitions:
            data = self.subcircuit_data[circuit_id].get("circuit", {}) or {}
            self.definitions[circuit_id] = GgcCircuit(self.name_of(circuit_id), data)
        return self.definitions[circuit_id]

    def check_not_recursive(self, circuit_id, placement):
        if circuit_id not in self.in_progress:
            return
        cycle = self.in_progress[self.in_progress.index(circuit_id):] + [circuit_id]
        raise CircuitError(
            component_id=placement.id, component_type=placement.ggc_type,
            component_label=self.name_of(circuit_id), error_code="recursiveSubcircuit",
            circuit_name=self.name_of(self.in_progress[-1]),
            chain=" → ".join(self.name_of(c) for c in cycle))

    def ensure_template(self, circuit_id, placement):
        """Returns the template variable for a subcircuit, emitting the template if this is
        its first use, or None if the subcircuit isn't defined"""
        if circuit_id in self.templates:
            return self.templates[circuit_id]
        if not self.has_definition(circuit_id):
            return None
        self.check_not_recursive(circuit_id, placement)
        self.in_progress.append(circuit_id)
        for comp in self.definition(circuit_id).components:
            comp.prepare(self)
        self.in_progress.pop()
        self.emit_template(circuit_id)
        return self.templates[circuit_id]

    def emit_template(self, circuit_id):
        definition = self.definition(circuit_id)
        circuit_variable = f"sub_{_sanitize(circuit_id)}"
        template_variable = f"Tmpl_{_sanitize(circuit_id)}"
        self.lines.append("")
        self.lines.append(f"# subcircuit: {definition.name}")
        self.lines.append(f'{circuit_variable} = circuit.Circuit(circuit_name="{_esc(definition.name)}")')
        CircuitEmitter(definition, circuit_variable, False, self).emit()
        self.lines.append(f"{template_variable} = circuit.Component({circuit_variable})")
        self.templates[circuit_id] = template_variable

    def emit_top(self):
        self.lines.append("")
        if self.top.name:
            self.lines.append(f'circuit0 = circuit.Circuit(circuit_name="{_esc(self.top.name)}")')
        else:
            self.lines.append("circuit0 = circuit.Circuit()")
        CircuitEmitter(self.top, "circuit0", True, self).emit()

    def emit_evaluations(self, is_async):
        for comp in self.top.components:
            statement = comp.evaluation_statement(is_async)
            if statement is not None:
                self.lines.append(statement)

    def emit_run(self):
        if self.mode == "test":
            self.emit_evaluations(is_async=False)
        elif self.mode == "test_async":
            self.emit_evaluations(is_async=True)
        elif self.mode == "run_async":
            self.lines.append("await circuit0.run_async()")
        else:
            self.lines.append("circuit0.run()")

    def generate(self):
        for comp in self.top.components:
            comp.prepare(self)
        self.emit_top()
        self.emit_run()
        return "\n".join(self.lines) + "\n"


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
    return ProgramGenerator(ggc, mode).generate()
