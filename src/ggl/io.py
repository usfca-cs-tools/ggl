from .node import Node, BitsNode
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

    def _resolve(self, nodes, name, kind, result):
        """Return the one node whose label == name. Labels are not guaranteed
        unique, so record a readable error for a missing or ambiguous name."""
        matches = [n for n in nodes if n.label == name]
        if not matches:
            result['errors'].append(f"{kind} '{name}' not found")
            return None
        if len(matches) > 1:
            result['errors'].append(
                f"{kind} '{name}' is ambiguous "
                f"({len(matches)} components share this label)")
            return None
        return matches[0]

    def evaluate(self, circuit):
        """Run every row of the truth table. Returns
        {label, passed, failures, errors} and emits it as a 'test' event."""
        result = {
            'label': self.label,
            'passed': False,
            'failures': [],
            'errors': [],
        }

        # Resolve the columns to circuit nodes once. A bad column name is an
        # error that applies to the whole table, so bail before running any row.
        in_nodes = [self._resolve(circuit.inputs, n, 'Input', result)
                    for n in self.input_names]
        out_nodes = [self._resolve(circuit.outputs, n, 'Output', result)
                     for n in self.output_names]
        if result['errors']:
            callbacks.emit('test', self.js_id, result)
            return result

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

        # Snapshot the driven inputs so the live circuit can be restored after the
        # run — otherwise its display is left stuck on the last row's vector,
        # which makes the on-canvas outputs disagree with the on-canvas inputs.
        saved_inputs = [(node, node.value) for node in in_nodes]

        for i, row in enumerate(self.rows, start=1):
            if len(row) != width:
                result['errors'].append(
                    f"Row {i} has {len(row)} values, expected {width}")
                continue
            in_vals, out_vals = row[:n_in], row[n_in:]
            drive(zip(in_nodes, in_vals))
            circuit.run()

            for name, node, expected in zip(
                    self.output_names, out_nodes, out_vals):
                actual = node.value
                if actual != expected:
                    in_desc = ", ".join(
                        f"{n}={v}" for n, v in zip(self.input_names, in_vals))
                    result['failures'].append(
                        f"For {in_desc}: expected {name}={expected}, "
                        f"got {actual}")

        # Restore inputs to their pre-test values and settle once, so the live
        # circuit's outputs reflect its authored inputs, not the last row.
        drive(saved_inputs)
        circuit.run()

        result['passed'] = not result['failures'] and not result['errors']
        logger.info(
            f"Test '{self.label}' {'passed' if result['passed'] else 'failed'}")
        callbacks.emit('test', self.js_id, result)
        return result
