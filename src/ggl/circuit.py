import asyncio
from collections import deque

from .edge import Edge
from .node import Node, Connector
from .ggl_logging import new_logger, set_global_js_logging
from .io import Input, Output, Clock
from .errors import CircuitError
from . import edge as edge_module

logger = new_logger(__name__)

MAX_ITERATIONS = 100  # Prevent runaway settle loops on unstable circuits

class Circuit:
    """
    Circuits are a collection of Nodes which may be run()
    to produce a value in Output Nodes
    """

    def __init__(self, label='', js_logging=None, circuit_name=None):
        self.label = label
        self.circuit_name = circuit_name
        # These are Nodes, NOT in/outpoints, pending a design for subcircuits
        self.inputs = []
        self.outputs = []
        # Ordered (insertion = connect() order) so cloning/propagation are
        # deterministic. A set here makes object iteration order vary per run,
        # which non-deterministically changes how feedback (latches) settle.
        self.all_nodes = []
        self.clock: Clock = None
        self.running = False  # set True by run()/run_async()
        # When True, assigning to an Input's .value re-propagates the circuit.
        self.auto_propagate = True
        # Re-entrancy guard so propagation can't trigger nested step()s.
        self._in_step = False

        # Set up logging for this circuit and all its components
        if js_logging is not None:
            set_global_js_logging(js_logging)

    def step(self):
        """
        Perform one synchronous propagation pass: seed the work queue with the
        circuit's inputs (the clock is an ordinary input) and propagate, each
        node firing at most once. Returns True if any node's value changed, so
        settle() can tell whether a fixpoint has been reached.
        """
        self._in_step = True
        # The synchronous engine bounds cycles with the visited set below, so
        # edges must always forward their value during a step.
        prev_gating = edge_module.gate_on_change
        edge_module.gate_on_change = False
        changed = False
        try:
            work = deque(self.inputs)
            visited = set()
            while work:
                node = work.popleft()
                if node in visited:
                    continue
                visited.add(node)
                before = getattr(node, 'value', None)
                try:
                    new_work = node.propagate()
                except CircuitError as e:
                    # Add circuit context if the error doesn't already carry it
                    if not e.circuit_name and self.circuit_name:
                        raise CircuitError(
                            component_id=e.component_id,
                            component_type=e.component_type,
                            component_label=e.component_label,
                            error_code=e.error_code,
                            severity=e.severity,
                            port_name=e.port_name,
                            connected_component_id=e.connected_component_id,
                            circuit_name=self.circuit_name,
                            **e.additional_fields
                        ) from e
                    raise
                if getattr(node, 'value', None) != before:
                    changed = True
                if new_work:
                    for n in new_work:
                        if n not in visited:
                            work.append(n)
        finally:
            edge_module.gate_on_change = prev_gating
            self._in_step = False
        return changed

    def settle(self):
        """
        Propagate until a full pass changes no node's value — the circuit's
        fixpoint for the current (static) inputs.

        step() reports whether anything changed; we repeat until it doesn't.
        Watching every node, not just the outputs, avoids stopping while an
        internal value is still in flight toward the outputs. Side-effect free
        (does not change `running` or advance the clock), so it is safe to call
        any time. The cap scales with circuit size so a deep-but-stable circuit
        isn't falsely flagged; reaching it means the circuit never settles
        (e.g. a combinational loop), which is warned.
        """
        cap = max(MAX_ITERATIONS, len(self.all_nodes) + 1)
        for _ in range(cap):
            if not self.step():
                return
        logger.warning("Circuit did not stabilize")

    def run(self):
        """
        Settle the circuit's combinational logic synchronously and return.

        This is the headless entry point. The free-running, real-time variant
        for the browser lives in run_async().
        """
        self.running = True
        self.settle()

    def cycle(self):
        """
        Advance the circuit by one clock cycle.

        Settles the combinational logic, then drives the clock low -> high ->
        low, settling each phase, so edge-triggered elements latch on the
        rising edge. Requires a connected Clock.
        """
        if self.clock is None:
            raise ValueError("cycle() requires a connected Clock")
        self.clock.value = 0
        self.settle()
        self.clock.value = 1
        self.settle()
        self.clock.value = 0
        self.settle()

    async def run_async(self):
        """
        Long-running asynchronous simulation for the browser/Pyodide: a
        free-running clock plus live input updates via update_input(), until
        stop() is called. Driven with `await circuit0.run_async()`.

        This is just real-time pacing over the same engine as the headless
        path: toggle the clock on its duty cycle and settle(); live input
        changes settle immediately in update_input().
        """
        self.running = True
        self.settle()
        while self.running:
            if self.clock is not None and self.clock.frequency:
                await asyncio.sleep(1 / self.clock.frequency / 2)
                self.clock.value = 1 - self.clock.value
                self.settle()
            else:
                await asyncio.sleep(0.1)

    def update_input(self, js_id, value):
        """Update an input node's value at runtime and re-settle the circuit."""
        for node in self.all_nodes:
            if node.js_id == js_id:
                node.value = value
                if self.running:
                    self.settle()
                logger.info(f"Updated input {js_id} to {value}")
                return
        logger.warning(f"Input node {js_id} not found")

    def stop(self):
        """Stop the running circuit simulation"""
        logger.info("Circuit stop() called")
        self.running = False
    
    def connect(self, src, dest, js_id=None):
        """
        Connect nodes using connectors or nodes directly.
        - For nodes with single outputs/inputs, can pass the node directly
        - For multiple outputs/inputs, must use node.output("name") or node.input("name")
        - For CircuitNode instances, automatically registers the node
        """

        # Determine source
        if isinstance(src, Connector):
            srcnode, srcname = src.node, src.name
        elif isinstance(src, Node):
            # Direct node - must have single output
            output_names = list(src.outputs.points.keys())
            if len(output_names) != 1:
                raise ValueError(
                    f"Node {src} has {len(output_names)} outputs, must use .output('name')")
            srcnode, srcname = src, output_names[0]
        else:
            raise TypeError("Source must be a Node or Connector")

        # Determine destination
        if isinstance(dest, Connector):
            destnode, destname = dest.node, dest.name
        elif isinstance(dest, Node):
            # Direct node - must have single input
            input_names = list(dest.inputs.points.keys())
            if len(input_names) != 1:
                raise ValueError(
                    f"Node {dest} has {len(input_names)} inputs, must use .input('name')")
            destnode, destname = dest, input_names[0]
        else:
            raise TypeError("Destination must be a Node or Connector")

        # Create the edge
        edge = Edge(srcnode, srcname, destnode, destname, js_id=js_id)
        srcnode.append_output_edge(srcname, edge)
        destnode.set_input_edge(destname, edge)

        # Wire up embedded circuits (CircuitNodes)
        # When connecting to a CircuitNode's input, replace its internal Input
        # with a ChildInput that reads from this edge
        from .component import CircuitNode
        if isinstance(destnode, CircuitNode):
            destnode._wire_child_input(destname, edge)

        # When connecting from a CircuitNode's output, replace its internal Output
        # with a ChildOutput that writes to this edge
        if isinstance(srcnode, CircuitNode):
            srcnode._wire_child_output(srcname, edge)

        # Seed the simulation from Input Nodes — and from the Clock, which is
        # an ordinary signal as far as propagation is concerned.
        if srcnode.kind in (Input.kind, Clock.kind) and srcnode not in self.inputs:
            self.inputs.append(srcnode)
            srcnode.circuit = self
        if destnode.kind == Output.kind and destnode not in self.outputs:
            self.outputs.append(destnode)

        for node in (srcnode, destnode):
            if node not in self.all_nodes:
                self.all_nodes.append(node)

        # Keep the Clock node for running the circuit
        if srcnode.kind == Clock.kind:
            # TODO error if multiple clocks
            self.clock = srcnode


def Component(circuit):
    """
    Create a reusable component template from a circuit definition.
    """
    def create_component_instance():
        # Create a new CircuitNode instance from the circuit template
        from .component import CircuitNode
        import time

        # Use timestamp + id for unique instance IDs
        instance_id = int(time.time() * 1000000) % 1000000
        return CircuitNode(circuit, instance_id)

    # Return a constructor that creates CircuitNode instances
    return create_component_instance
