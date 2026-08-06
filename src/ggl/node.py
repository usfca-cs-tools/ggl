import copy
import logging

from .ggl_logging import new_logger
from .errors import CircuitError

logger = new_logger(__name__)


class BitWidthMismatch(Exception):
    """Carry these details internally to GGL exception handler"""

    def __init__(self, expected, actual):
        self.expected = expected
        self.actual = actual


class Connector:
    """Represents a specific input or output point on a node"""

    def __init__(self, node, name):
        self.node = node
        self.name = name


class NodeInputs:
    """
    The inputs to a Node are a dict of name-to-edge.
    This dict is one-to-one because an input must be fed by
    exactly one Edge.
    NB: This is NOT an Input Node! (those are in io.py)
    """

    def __init__(self, names, node):
        self.points = {name: None for name in names}
        self.node = node

    def get_edge(self, name):
        return self.points.get(name)

    def get_edges(self):
        return [edge for _, edge in self.points.items()]

    def set_edge(self, name, edge):
        # An input is fed by exactly one edge. A second, different edge on the
        # same port is two wires driving one input — a short circuit. Silently
        # keeping the last one hides the mistake; raise so it's visible.
        existing = self.points[name]
        if existing is not None and existing is not edge:
            node = self.node
            driver = existing.srcpoint.node
            raise CircuitError(
                component_id=node.js_id,
                component_type=node.error_kind,
                component_label=node.label,
                error_code="inputShortCircuit",
                port_name=name,
                connected_component_id=getattr(driver, 'js_id', None),
            )
        self.points[name] = edge

    def get_names(self):
        return self.points.keys()

    def read_value(self, name, bits):
        # Read the value from the edge connected to the named inpoint
        edge = self.points[name]
        # edge.bits is None means unvisited, so don't raise for that
        if edge.bits is not None and bits != edge.bits:
            raise BitWidthMismatch(bits, edge.bits)
        return edge.value

    def __getitem__(self, index):
        logger.info(f'getitem index: {index}')
        """Allow array-style access like node.inputs[0]"""
        names = list(self.points.keys())
        if 0 <= index < len(names):
            return Connector(self.node, names[index])
        raise IndexError(f"Input index {index} out of range")


class NodeOutputs:
    """
    The outputs from a node are a dict of name-to-list-of-Edge.
    This dict is one-to-many because an output's value may
    feed many Edges
    NB: This is NOT an Output Node (those are in io.py)
    """

    def __init__(self, names, node):
        self.points = {name: [] for name in names}
        self.node = node

    def append_edge(self, name, obj):
        self.points[name].append(obj)

    def get_names(self):
        return self.points.keys()

    def write_value(self, name, value, bits):
        # Write the given value to all edges connected to the named outpoint
        # Add all nodes connectes to those edges to the simulator work list
        new_work = []
        for edge in self.points[name]:
            new_work += edge.propagate(value=value, bits=bits)
        return new_work

    def __getitem__(self, index):
        """Allow array-style access like node.outputs[0]"""
        names = list(self.points.keys())
        if 0 <= index < len(names):
            return Connector(self.node, names[index])
        raise IndexError(f"Output index {index} out of range")


class Node:
    """
    Nodes are elements (gates, adders, plexers, registers, subcircuits)
    in the circuit
    """

    def __init__(self, kind, js_id='', innames=[], outnames=[], label=''):
        self.kind = kind    # Hard-coded, e.g. 'And Gate'
        self.js_id = js_id  # Optionally provided be the frontend
        self.label = label  # User-provided, e.g. 'is b-type'
        self.inputs = NodeInputs(innames, self)
        self.outputs = NodeOutputs(outnames, self)

    def __str__(self):
        return f'{self.kind} {self.label}'

    def get_input_edge(self, name):
        return self.inputs.get_edge(name)

    def set_input_edge(self, name, edge):
        self.inputs.set_edge(name, edge)

    @property
    def error_kind(self):
        """The component type reported to the user in errors. Defaults to the
        node's kind; internal specializations (e.g. ChildOutput) override this
        to surface their user-facing type (Output) instead of the wrapper."""
        return self.kind

    def safe_read_input(self, iname, bits=1):
        """If iname is not connected raise an exception through to the UI"""
        try:
            return self.inputs.read_value(iname, bits)
        except AttributeError as ae:
            raise CircuitError(
                component_id=self.js_id,
                component_type=self.error_kind,
                component_label=self.label,
                error_code="inputNotConnected",
                port_name=iname
            ) from ae
        except BitWidthMismatch as bwm:
            raise CircuitError(
                component_id=self.js_id,
                component_type=self.error_kind,
                component_label=self.label,
                error_code="bitWidthMismatch",
                expectedBits=bwm.expected,
                actualBits=bwm.actual,
                port_name=iname
            ) from bwm

    def append_output_edge(self, name, edge):
        self.outputs.append_edge(name, edge)

    def preflight(self):
        """Verify this node is ready to simulate, before a run begins. Base check:
        every input port must be connected. Subclasses override for special needs
        (e.g. a genuinely optional input). Raises CircuitError(inputNotConnected) —
        the same error reading an unconnected input raises — so an open input
        surfaces through the one structured-error path whether it's caught here or
        during propagation."""
        for name in self.inputs.get_names():
            if self.inputs.get_edge(name) is None:
                raise CircuitError(
                    component_id=self.js_id,
                    component_type=self.error_kind,
                    component_label=self.label,
                    error_code="inputNotConnected",
                    port_name=name)

    def input(self, name):
        """Returns a Connector for the named input"""
        return Connector(self, name)

    def output(self, name):
        """Returns a Connector for the named output"""
        return Connector(self, name)

    def __getattribute__(self, name):
        """
        Enable attribute-style access to inputs and outputs, with port names taking
        priority over class attributes (so adder.a is the 'a' port, not the class
        constant Adder.a == 'a').
        Examples: mux.sel instead of mux.input("sel"), adder.sum instead of adder.output("sum")

        This runs on EVERY attribute access, so it is kept lean: one __dict__ fetch and
        an O(1) dict-membership test per port map — no get_names()/keys() view built per
        access, which previously dominated simulation time. Reads inputs/outputs from
        __dict__ so an access during construction (before they're set) can't recurse.
        """
        ga = object.__getattribute__
        d = ga(self, '__dict__')
        inputs = d.get('inputs')
        if inputs is not None and name in inputs.points:
            return Connector(self, name)
        outputs = d.get('outputs')
        if outputs is not None and name in outputs.points:
            return Connector(self, name)
        return ga(self, name)

    def propagate(self, output_name='0', value=0, bits=0):
        """
        The base Node propagate() method fans out the given value to the
        given output_name, assuming all necessary transformations (e.g.
        invert, truncation) have been done by propagate() in derived classes
        """
        assert (output_name in self.outputs.points)
        logger.info(
            f"{self.kind} '{self.label}' output '{output_name}' propagates {hex(value)}")
        return self.outputs.write_value(output_name, value, bits)

    def __deepcopy__(self, memo):
        """Deep-copy this node's OWN state only, never its edges.

        An edge references the nodes it joins and a node's `.circuit` points back at the
        whole Circuit, so a naive deepcopy walks the entire connected graph — O(graph) per
        node and deep enough on large nested circuits to overflow the recursion limit.
        Instead we copy the intrinsic attributes (bits, ROM/register contents, js_id, …)
        and give the copy fresh, empty port structures + no circuit back-reference, so the
        clone is disconnected by construction. Because EVERY node deep-copies this way, a
        reference that does survive (a ChildInput's `parent_edge`) can only reach adjacent
        nodes, which in turn drop their own edges — the traversal can't run away.
        """
        new = self.__class__.__new__(self.__class__)
        memo[id(self)] = new
        for key, value in self.__dict__.items():
            if key == "inputs":
                new.inputs = NodeInputs(list(value.points.keys()), new)
            elif key == "outputs":
                new.outputs = NodeOutputs(list(value.points.keys()), new)
            elif key == "circuit":
                new.circuit = None  # back-reference; the caller reassigns it
            else:
                new.__dict__[key] = copy.deepcopy(value, memo)
        return new

    def clone(self, instance_id):
        """Create a disconnected copy of this node with an instance-suffixed label.
        Connections are cleared by __deepcopy__; the caller rewires the clone."""
        node = copy.deepcopy(self)
        if getattr(node, "label", None):
            node.label = f"{node.label}_{instance_id}"
        return node


class BitsNode(Node):
    """
    BitsNode is a Node which has a bit width, e.g. Gates, plexers, registers
    """

    def __init__(self, kind, js_id='', num_inputs=0, num_outputs=0, label='', bits=1, named_inputs=[], named_outputs=[]):
        # Inputs can be numbered (num_inputs) or named (named_inputs, e.g. 'D', 'en')
        innames = [str(i) for i in range(num_inputs)]
        innames += named_inputs

        # Outputs can be numbered (num_outputs) or named (named_outputs, e.g. 'Q', 'R')
        outnames = [str(o) for o in range(num_outputs)]
        outnames += named_outputs

        super().__init__(kind, js_id=js_id, innames=innames, outnames=outnames, label=label)
        self.bits = bits

    def mask(self, bits=None):
        """
        Builds the bit mask for the number of data bits for this gate
        Use provided bits if specified, otherwise default to self.bits
        """
        if bits is None:
            bits = self.bits
        return (1 << bits) - 1

    def propagate(self, output_name='0', value=0, bits=None):
        if bits is None:
            bits = self.bits
        value &= self.mask(bits)
        return super().propagate(output_name=output_name, value=value, bits=bits)

    def safe_read_input(self, iname, bits=None):
        """
        Override safe_read_input() for nodes where multi-bit inputs are expected
        The base class raises an exception if the input Edge.bits doesn't match
        """
        if bits is None:
            bits = self.bits
        return super().safe_read_input(iname, bits=bits)
