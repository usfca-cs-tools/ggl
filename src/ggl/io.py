from .node import Node, BitsNode
from .errors import CircuitError
from .ggl_logging import new_logger
from . import callbacks

logger = new_logger(__name__)


class IONode(BitsNode):
    """
    IONode is an abstract class which encapsulates the value of an I/O node
    """

    def __init__(self, kind, num_inputs, num_outputs, js_id='', label='', bits=1):
        super().__init__(
            kind=kind,
            js_id=js_id,
            num_inputs=num_inputs,
            num_outputs=num_outputs,
            label=label,
            bits=bits)
        self._value = 0
        # Set by Circuit.connect() for top-level Input nodes so that assigning
        # to .value can re-propagate the circuit (dynamic inputs at runtime).
        self.circuit = None

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, new_value):
        self._value = new_value


class Input(IONode):
    """
    Input is an IONode for the input of a circuit, e.g. A
    """

    kind = 'Input'

    def __init__(self, js_id='', label='', bits=1):
        super().__init__(
            kind=Input.kind,
            js_id=js_id,
            num_inputs=0,
            num_outputs=1,
            label=label,
            bits=bits)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, new_value):
        """Assigning a new value re-propagates the circuit (dynamic inputs).

        This is what lets a test set inputs after run() and read updated
        outputs without an explicit step(). The _in_step guard prevents
        re-entrancy when the value is changed during propagation itself.
        """
        if self._value != new_value:
            self._value = new_value
            c = self.circuit
            if (c is not None and getattr(c, 'auto_propagate', False)
                    and c.running and not c._in_step):
                c.step()

    def propagate(self, output_name='0', value=0):
        return super().propagate(value=self.value)


class ChildInput(Input):
    """
    ChildInput is an Input node inside an embedded circuit (CircuitNode).
    It reads its value from a parent circuit's edge rather than from a UI input.

    When propagate() is called, it:
    1. Copies the value from the parent edge into self.value
    2. Propagates that value to the child circuit's internal edges
    """

    kind = 'ChildInput'

    # User-facing type in error messages: a ChildInput is an Input to the user.
    error_kind = 'Input'

    def __init__(self, parent_edge, js_id='', label='', bits=1):
        super().__init__(js_id=js_id, label=label, bits=bits)
        self.parent_edge = parent_edge

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, new_value):
        # Plain assignment: a ChildInput is driven by its parent edge during
        # propagation, so it must NOT trigger the dynamic-input re-propagation
        # that top-level Input does.
        self._value = new_value

    def propagate(self, output_name='0', value=0):
        # Read value from parent circuit's edge
        self.value = self.parent_edge.value
        # Propagate to child circuit's internal nodes
        return super().propagate()


class Output(IONode):
    """
    Output is an IONode for the output of a circuit, e.g. R
    """

    kind = 'Output'

    def __init__(self, js_id='', label='', bits=1):
        super().__init__(
            kind=Output.kind,
            js_id=js_id,
            num_inputs=1,
            num_outputs=0,
            label=label,
            bits=bits)

    def propagate(self, output_name='0', value=0):
        self.value = self.safe_read_input('0')
        logger.info(f"{self.kind} '{self.label}' gets value {self.value}")
        callbacks.emit('value', self.js_id, self.value)


class ChildOutput(Output):
    """
    ChildOutput is an Output node inside an embedded circuit (CircuitNode).
    It writes its value to parent circuit edges rather than to a UI output.

    Unlike Output (which has num_outputs=0 as a terminal node), ChildOutput
    has num_outputs=1 to leverage the existing NodeOutputs fan-out infrastructure.
    Multiple parent edges can be added via append_output_edge('0', edge).

    When propagate() is called, it:
    1. Reads its value from the child circuit's internal edge
    2. Propagates that value to ALL parent circuit's downstream nodes
    """

    kind = 'ChildOutput'

    # User-facing type in error messages: a ChildOutput is an Output to the user.
    error_kind = 'Output'

    def __init__(self, js_id='', label='', bits=1):
        # Override Output's num_outputs=0 with num_outputs=1
        # This gives us an output point '0' with a list for fan-out
        IONode.__init__(self,
            kind=ChildOutput.kind,
            js_id=js_id,
            num_inputs=1,
            num_outputs=1,
            label=label,
            bits=bits)

    def propagate(self, output_name='0', value=0):
        # Read value from child circuit's internal edge
        self.value = self.safe_read_input('0')
        logger.info(f"{self.kind} '{self.label}' gets value {self.value}")
        # Use normal NodeOutputs fan-out to propagate to all parent edges
        return self.outputs.write_value('0', self.value, self.bits)


class Constant(Input):
    """
    Constant is an IONode for constant values in a circuit, e.g. c0001093
    Maybe it's odd to make it an alias for Input, but for simulation
    purposes, it seems to behave like an input
    """

    def __init__(self, js_id='', label='', bits=1):
        super().__init__(js_id=js_id, label=label, bits=bits)


class Clock(IONode):
    kind = 'Clock'

    def __init__(self, js_id='', label='', frequency=0, mode="auto"):
        super().__init__(
            Clock.kind,
            js_id=js_id,
            num_inputs=0,
            num_outputs=1,
            label=label,
            bits=1
        )
        self.frequency = frequency
        self.mode = mode

    def propagate(self, output_name='0', value=0):
        return super().propagate(value=self.value)


class Test(Node):
    """
    A Test is a verification directive rather than a circuit element: it is a
    truth table. It names Input columns and Output columns (by component label),
    and holds one row per test vector. For each row it drives the named Inputs to
    that row's values, settles the circuit, and checks the named Outputs against
    that row's expected values.

    Data model:
      input_names:  ['A', 'B']            (labels of Input components)
      output_names: ['Y']                 (labels of Output components)
      rows:         [[0, 0, 0],           (each row is input values then output
                     [0, 1, 0],            values, in column order:
                     [1, 0, 0],            input_names + output_names)
                     [1, 1, 1]]

    evaluate(circuit) is the single source of truth for pass/fail. It returns a
    structured result AND emits a 'test' callback, so the identical logic serves
    both the headless grading path (read the return value) and the interactive
    editor (the callback badges the component). This handles combinational
    circuits (each row is an independent settle); clocked/sequential support is a
    planned follow-on.
    """

    kind = 'Test'

    def __init__(self, label='', js_id='', input_names=None, output_names=None,
                 rows=None):
        # list(... or []) copies and avoids the shared-mutable-default footgun.
        self.input_names = list(input_names or [])
        self.output_names = list(output_names or [])
        self.rows = [list(r) for r in (rows or [])]
        super().__init__(kind=Test.kind, js_id=js_id, label=label)

    def _resolve(self, nodes, name, not_found_code):
        """Return the one circuit node whose label == name, or raise a
        CircuitError for a missing or ambiguous name. Labels are not guaranteed
        unique, so an ambiguous name is a distinct error."""
        matches = [n for n in nodes if n.label == name]
        if not matches:
            self._raise(not_found_code, name=name)
        if len(matches) > 1:
            self._raise('testAmbiguousLabel', name=name)
        return matches[0]

    def _raise(self, error_code, **fields):
        """Signal a failing Test the same way the engine signals open inputs and
        bit-width mismatches: badge the component as failed, then raise a
        CircuitError carrying an error_code + fields for the front end to localize
        and display (highlight + message)."""
        callbacks.emit('test', self.js_id, {'label': self.label, 'passed': False})
        raise CircuitError(
            component_id=self.js_id,
            component_type=Test.kind,
            component_label=self.label,
            error_code=error_code,
            **fields)

    def evaluate(self, circuit):
        """Run the truth table one row at a time. On the FIRST problem — a bad
        column name, a malformed row, or a failing row — restore the circuit's
        inputs and raise a CircuitError, which the front end surfaces exactly like
        an open-input or bit-width error (component highlighted + localized
        message). On success, emit a 'test' pass event (for the badge) and return.

        Surfacing one problem at a time matches the rest of the app's error model.
        (The aggregate 'report every failing row' behavior that headless grading
        will want is a planned follow-on.)
        """
        # Resolve columns first; nothing is driven yet, so there's nothing to
        # restore if a name is bad.
        in_nodes = [self._resolve(circuit.inputs, n, 'testInputNotFound')
                    for n in self.input_names]
        out_nodes = [self._resolve(circuit.outputs, n, 'testOutputNotFound')
                     for n in self.output_names]

        n_in = len(self.input_names)
        width = n_in + len(self.output_names)
        prev_auto = circuit.auto_propagate

        def drive(pairs):
            # Set inputs with auto-propagate off so the vector settles once, via
            # the explicit run() that follows.
            circuit.auto_propagate = False
            try:
                for node, value in pairs:
                    node.value = value
            finally:
                circuit.auto_propagate = prev_auto

        # Snapshot inputs so the live circuit can be returned to its authored
        # state whether the test passes or fails — otherwise the on-canvas outputs
        # would be left showing some test row's values.
        saved_inputs = [(node, node.value) for node in in_nodes]

        def restore():
            drive(saved_inputs)
            circuit.run()

        for i, row in enumerate(self.rows, start=1):
            if len(row) != width:
                restore()
                self._raise('testRowWidth', row=i, actual=len(row),
                            expected=width)
            in_vals, out_vals = row[:n_in], row[n_in:]
            drive(zip(in_nodes, in_vals))
            circuit.run()

            for name, node, expected in zip(
                    self.output_names, out_nodes, out_vals):
                actual = node.value
                if actual != expected:
                    in_desc = ", ".join(
                        f"{n}={v}" for n, v in zip(self.input_names, in_vals))
                    restore()
                    self._raise('testFailed', inputs=in_desc, output=name,
                                expected=expected, actual=actual)

        # Every row passed: leave the circuit at its authored inputs and badge it.
        restore()
        logger.info(f"Test '{self.label}' passed")
        callbacks.emit('test', self.js_id, {'label': self.label, 'passed': True})
        return {'label': self.label, 'passed': True}
