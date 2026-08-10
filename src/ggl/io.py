import asyncio
import time

from .node import Node, BitsNode
from .errors import CircuitError
from .ggl_logging import new_logger
from . import callbacks

logger = new_logger(__name__)

# How often the cooperative (browser) test loop yields to the event loop while
# pulsing a clocked Test. ~30 fps: often enough that the UI stays responsive,
# live updates render, Stop is honored, and the queued highlight timers drain;
# rare enough that yielding doesn't dominate a long processor run.
_TEST_YIELD_INTERVAL_S = 1 / 30


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
        # A subcircuit port must be driven at its own width: otherwise the value is silently
        # re-widthed at the boundary and a real wiring mistake (a 64-bit bus into an 8-bit
        # port, a 4-bit signal onto a 1-bit clock) goes unreported. Enforce an exact match,
        # like a direct wire does. bits is None until the parent has driven the edge once.
        if self.parent_edge.bits is not None and self.parent_edge.bits != self.bits:
            raise CircuitError(
                component_id=self.js_id,
                component_type=self.error_kind,
                component_label=self.label,
                error_code="bitWidthMismatch",
                port_name=self.label,
                expectedBits=self.bits,
                actualBits=self.parent_edge.bits)
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
        # Emit as a string: a 64-bit value exceeds JS Number's exact range (2**53) and it crosses
        # to the UI via json.dumps -> JSON.parse. A string survives exactly; the UI parses it with
        # BigInt. (RV64 needs full 64-bit display, e.g. -1 == 0xFFFFFFFFFFFFFFFF.)
        callbacks.emit('value', self.js_id, str(self.value))


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
    Constant is a fixed-value source. For a circuit's own simulation it behaves exactly
    like an Input (it seeds propagation with its value), so it reuses Input's machinery —
    but it has its own kind because it is NOT an external interface port: its value is set
    once at construction, so a parent circuit can't drive it, and a subcircuit must not
    expose it as a port (see component.CircuitNode).
    """

    kind = 'Constant'

    def __init__(self, js_id='', label='', bits=1):
        super().__init__(js_id=js_id, label=label, bits=bits)
        self.kind = Constant.kind  # Input.__init__ set it to 'Input'; override


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
    editor (the callback badges the component).

    Two modes:
      - combinational (default): each row is set-inputs, settle, check-outputs.
      - clocked (stop_enabled): each row sets the input columns as the initial
        state, then pulses the clock until the stop_output_name output reaches
        stop_output_value (capped at max_cycles), then checks the output columns
        at that point. For sequential circuits — e.g. a processor running until a
        DONE signal goes high.
    """

    kind = 'Test'

    def __init__(self, label='', js_id='', input_names=None, output_names=None,
                 rows=None, stop_enabled=False, stop_output_name='',
                 stop_output_value=1, max_cycles=10000,
                 reset_enabled=False, reset_input_name='', reset_value=1,
                 reset_cycles=1):
        # list(... or []) copies and avoids the shared-mutable-default footgun.
        self.input_names = list(input_names or [])
        self.output_names = list(output_names or [])
        self.rows = [list(r) for r in (rows or [])]
        # Clocked mode: pulse the clock until an output reaches a value, then
        # check the expected outputs. max_cycles is a safety cap (not surfaced in
        # the UI); the other three are set from the property panel.
        self.stop_enabled = bool(stop_enabled)
        self.stop_output_name = stop_output_name or ''
        self.stop_output_value = stop_output_value
        self.max_cycles = max_cycles
        # Reset pulse: before each run, drive a named input to reset_value for
        # reset_cycles clock cycles, then to 0 — so a sequential circuit starts
        # from a known state. reset_value/reset_cycles are defaults, not UI.
        self.reset_enabled = bool(reset_enabled)
        self.reset_input_name = reset_input_name or ''
        self.reset_value = reset_value
        self.reset_cycles = reset_cycles
        super().__init__(kind=Test.kind, js_id=js_id, label=label)

    def _resolve(self, nodes, name, not_found_code):
        """Return the one circuit node whose label == name, or raise a
        CircuitError for a missing or ambiguous name. Labels are not guaranteed
        unique, so an ambiguous name is a distinct error."""
        matches = [n for n in nodes if n.label == name]
        if not matches:
            # Include the labels the circuit *does* expose so a mis-named test
            # column tells the author (or student) exactly what to match.
            self._raise(not_found_code, name=name,
                        available=sorted(n.label for n in nodes if n.label))
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

    def _evaluate_steps(self, circuit):
        """The truth-table run, as a generator that `yield`s after each clock
        cycle of a clocked Test — the one place a long run needs a cooperative
        breather. The two public entry points drive it:

          - evaluate() (headless/CPython) exhausts it, ignoring the yields.
          - evaluate_async() (browser) yields to the event loop on a throttle and
            aborts on circuit.stop_requested.

        Because every yield is a point where the generator can be closed, the
        clocked loop is wrapped so an abort (gen.close() -> GeneratorExit) still
        restores the circuit to its authored inputs, exactly like a pass/fail.

        On the FIRST problem — a bad column name, a malformed row, or a failing
        row — it restores and raises a CircuitError, surfaced by the front end
        exactly like an open-input or bit-width error. On success it emits a
        'test' pass event (for the badge) and returns the result dict.
        """
        # Resolve columns first; nothing is driven yet, so there's nothing to
        # restore if a name is bad.
        in_nodes = [self._resolve(circuit.inputs, n, 'testInputNotFound')
                    for n in self.input_names]
        out_nodes = [self._resolve(circuit.outputs, n, 'testOutputNotFound')
                     for n in self.output_names]

        # Clocked features (stop condition and/or a reset pulse) need a clock.
        stop_node = None
        reset_node = None
        if self.stop_enabled or self.reset_enabled:
            if circuit.clock is None:
                self._raise('testNoClock')
        if self.stop_enabled:
            stop_node = self._resolve(
                circuit.outputs, self.stop_output_name, 'testOutputNotFound')
        if self.reset_enabled:
            reset_node = self._resolve(
                circuit.inputs, self.reset_input_name, 'testInputNotFound')

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

        # A clocked run (reset and/or stop) with no expected-output rows is still
        # a valid "reset, then run until the stop condition" — execute one implicit
        # scenario instead of skipping everything. (Combinational tests with no
        # rows correctly do nothing.)
        rows = self.rows
        if not rows and (self.stop_enabled or self.reset_enabled):
            rows = [[]]

        try:
            for i, row in enumerate(rows, start=1):
                if len(row) != width:
                    restore()
                    self._raise('testRowWidth', row=i, actual=len(row),
                                expected=width)
                in_vals, out_vals = row[:n_in], row[n_in:]
                drive(zip(in_nodes, in_vals))
                circuit.run()

                # Reset pulse: assert the reset input, clock it in, then deassert
                # — so a sequential circuit starts from a known state.
                if self.reset_enabled:
                    drive([(reset_node, self.reset_value)])
                    for _ in range(self.reset_cycles):
                        circuit.cycle()
                    drive([(reset_node, 0)])
                    circuit.run()

                # Clocked: pulse the clock until the stop output reaches its value
                # (or give up), then sample the outputs at that point.
                stop_desc = ""
                if self.stop_enabled:
                    cycles = 0
                    while (stop_node.value != self.stop_output_value
                            and cycles < self.max_cycles):
                        circuit.cycle()
                        cycles += 1
                        yield  # cooperative breather (see evaluate_async)
                    if stop_node.value != self.stop_output_value:
                        restore()
                        self._raise('testStopNotReached',
                                    name=self.stop_output_name,
                                    value=self.stop_output_value,
                                    cycles=self.max_cycles)
                    stop_desc = f"{self.stop_output_name}={self.stop_output_value}"

                for name, node, expected in zip(
                        self.output_names, out_nodes, out_vals):
                    actual = node.value
                    if actual != expected:
                        parts = [f"{n}={v}"
                                 for n, v in zip(self.input_names, in_vals)]
                        if stop_desc:
                            parts.append(stop_desc)
                        in_desc = ", ".join(parts)
                        restore()
                        self._raise('testFailed', inputs=in_desc, output=name,
                                    expected=expected, actual=actual)
        except GeneratorExit:
            # evaluate_async closed us mid-run (user hit Stop): leave the circuit
            # at its authored inputs, same as a normal finish, then unwind.
            restore()
            raise

        # Every row passed: leave the circuit at its authored inputs and badge it.
        restore()
        logger.info(f"Test '{self.label}' passed")
        callbacks.emit('test', self.js_id, {'label': self.label, 'passed': True})
        return {'label': self.label, 'passed': True}

    def evaluate(self, circuit):
        """Synchronous run (headless CPython / pytest / the mode='test' codegen).

        Drives _evaluate_steps to completion, ignoring its cooperative yields, so
        behavior is identical to the pre-cooperative engine: return the result
        dict, or propagate the CircuitError raised on the first problem.
        """
        gen = self._evaluate_steps(circuit)
        try:
            while True:
                next(gen)
        except StopIteration as done:
            return done.value

    async def evaluate_async(self, circuit):
        """Cooperative run for the browser (mode='test_async' codegen).

        Same checks and result as evaluate(), but between clock cycles it yields
        to the event loop on a throttle so the UI stays responsive, live updates
        render, the queued highlight timers drain, and a Stop (circuit.stop_requested)
        is honored — restoring the circuit and returning a 'cancelled' result
        rather than a pass/fail. Once stop is requested, every remaining Test in
        the program short-circuits here without touching the circuit.
        """
        if circuit.stop_requested:
            return {'label': self.label, 'passed': None, 'cancelled': True}
        gen = self._evaluate_steps(circuit)
        last_yield = time.perf_counter()
        try:
            while True:
                next(gen)
                if circuit.stop_requested:
                    gen.close()  # -> GeneratorExit in _evaluate_steps -> restore()
                    return {'label': self.label, 'passed': None,
                            'cancelled': True}
                now = time.perf_counter()
                if now - last_yield >= _TEST_YIELD_INTERVAL_S:
                    await asyncio.sleep(0)
                    last_yield = now
        except StopIteration as done:
            return done.value
