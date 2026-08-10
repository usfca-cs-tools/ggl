from .node import BitsNode
from .ggl_logging import new_logger

logger = new_logger(__name__)


class WireNode(BitsNode):
    """
    Class for wire type components such as splitters, mergers, and tunnels.
    """

    def __init__(self, kind, num_inputs, num_outputs, js_id='', label='', bits=1):
        super().__init__(
            kind,
            js_id=js_id,
            num_inputs=num_inputs,
            num_outputs=num_outputs,
            label=label,
            bits=bits
        )


class Splitter(WireNode):
    """
    Splits multi bit input into individual 1 bit outputs
    """
    kind = 'Splitter'
    # TODO: error checking if the splits don't add up to total input bits

    def __init__(self, js_id='', label='', bits=1, splits=None):
        if splits is None:
            # default to 1-bit outputs
            splits = [(i, i) for i in range(bits)]
        self.splits = splits
        super().__init__(Splitter.kind, js_id=js_id, num_inputs=1,
                         num_outputs=len(splits), label=label, bits=bits)

    def propagate(self, output_name='0', value=0):
        input_value = self.safe_read_input('0', bits=self.bits)

        new_work = []
        for i, (start, end) in enumerate(self.splits):
            low = min(start, end)
            high = max(start, end)
            width = high - low + 1
            mask = (1 << width) - 1
            # mask bit at i to get bit_val, and then propagate to the output node
            chunk = (input_value >> low) & mask

            logger.info(
                f'{self.kind} {self.label} output {i} ({start}-{end}): {bin(chunk)}')
            new_work += super().propagate(output_name=str(i), value=chunk, bits=width)

        return new_work


class Merger(WireNode):
    """
    Merges multiple 1 bit inputs into a single output
    """
    kind = 'Merger'

    def __init__(self, js_id='', label='', bits=1, merge_inputs=None):
        if merge_inputs is None:
            # default to 1-bit inputs
            merge_inputs = [(i, i) for i in range(bits)]

        self.merge_inputs = merge_inputs
        super().__init__(Merger.kind, js_id=js_id, num_inputs=len(
            merge_inputs), num_outputs=1, label=label, bits=bits)

    def propagate(self, output_name='0', value=0):
        output_val = 0

        for i, (start, end) in enumerate(self.merge_inputs):
            low = min(start, end)
            high = max(start, end)
            width = high - low + 1

            v = self.safe_read_input(str(i), bits=width)
            # mask to width and shift into position
            # or all bits together to get an output value
            masked = v & ((1 << width) - 1)
            output_val |= (masked << low)
        return super().propagate(value=output_val)


class Tunnel(WireNode):
    """
    Tunnels a value from input to output, used for readability and to reduce wiring
    """
    kind = 'Tunnel'

    def __init__(self, js_id='', label='', direction='input', **kwargs):
        # A tunnel is a named wire and has no width of its own — it carries whatever the
        # net's driver produces. (A stray `bits` from an older .ggc lands in **kwargs and
        # is ignored.)
        super().__init__(
            Tunnel.kind,
            js_id=js_id,
            num_inputs=1 if direction == 'input' else 0,
            num_outputs=1 if direction == 'output' else 0,
            label=label
        )

        self.direction = direction
        # Owning Circuit, set by Circuit.connect() (top level) or _clone_circuit() (embedded
        # instance). Same-label tunnels join within THIS circuit only (see propagate()), so a
        # subcircuit's internal tunnels resolve among its own nodes — not through a global
        # registry, which cloning bypassed and which would cross-talk between instances.
        self.circuit = None

    def is_output_tunnel(self):
        return self.direction == 'output'
    
    def propagate(self, output_name='0', value=0):
        if not self.label or self.direction != 'input' or self.circuit is None:
            return []

        # Read the driving edge at whatever width the driver produced and republish it to
        # every same-label subscriber IN THIS CIRCUIT. The tunnel imposes no width of its
        # own, so the value crosses intact; a genuine width mismatch surfaces where the net
        # is finally read.
        edge = self.inputs.get_edge('0')
        if edge is None or edge.bits is None:
            return []  # the net has no driver yet on this pass

        logger.info(f'{self.kind} {self.label} (input): publishing {bin(edge.value)} ({edge.bits} bits)')
        work = []
        for sink in self._sinks_for(self.circuit).get(self.label, []):
            work += sink.receive(edge.value, edge.bits)
        return work

    @staticmethod
    def _sinks_for(circuit):
        """Map label -> [output tunnels] for `circuit`, built once and cached on the circuit.
        The tunnel graph is fixed once construction/connect finish (before any run), so a
        single scan of all_nodes suffices — no per-pass rescans, no process-global state."""
        sinks = getattr(circuit, '_tunnel_sinks', None)
        if sinks is None:
            sinks = {}
            for node in circuit.all_nodes:
                if getattr(node, 'kind', None) == 'Tunnel' and node.direction == 'output' and node.label:
                    sinks.setdefault(node.label, []).append(node)
            circuit._tunnel_sinks = sinks
        return sinks

    def receive(self, value, bits):
        if self.direction != 'output':
            logger.warning(f'{self.kind} {self.label}: receive() called on non-output tunnel')
            return []
        return super().propagate(output_name='0', value=value, bits=bits)
