import random

from .node import BitsNode
from .ggl_logging import new_logger
from . import callbacks

logger = new_logger(__name__)


class Clockable:
    """Mixin for nodes that act on a rising CLK edge (Register, RegisterClr, RAM).
    settle() calls propagate() repeatedly until the circuit stops changing, so a clocked
    node must latch on the CLK 0->1 EDGE, not while CLK is merely high
    """
    CLK = 'CLK'
    _prev_clk = 0  # last CLK level seen; class default, shadowed per-instance on first write

    def clock_edge(self):
        """Read CLK and return (level, rising); `rising` is True only on the pass where CLK
        goes 0->1, exactly once per propagate()
        """
        clk = self.safe_read_input(Clockable.CLK, bits=1)
        rising = self._prev_clk == 0 and clk == 1
        self._prev_clk = clk
        return clk, rising


class Register(Clockable, BitsNode):
    D = 'D'
    en = 'en'
    Q = 'Q'
    kind = 'Register'

    def __init__(self, js_id='', label='', bits=32):
        super().__init__(
            kind=Register.kind,
            js_id=js_id,
            label=label,
            bits=bits,
            named_inputs=[Register.D, Register.CLK, Register.en],
            named_outputs=[Register.Q])
        self.value = random.getrandbits(bits)  # initial state is random

    def propagate(self, output_name='Q', value=0):
        _, rising = self.clock_edge()
        en = self.safe_read_input(Register.en, bits=1)
        if rising and en:
            self.value = self.safe_read_input(Register.D)
        return super().propagate(output_name=output_name, value=self.value)

    # Nothing special to do for clone(). BitsNode.clone() is enough.


class RegisterClr(Clockable, BitsNode):
    """Edge-triggered register with an asynchronous clear.

    Ports: D, CLK, en, CLR -> Q.

    - CLR is asynchronous and dominant: while CLR is high, Q is forced to 0
      immediately, independent of the clock or enable. This is how a running
      circuit is driven to a known initial state without pulsing the clock.
    - Otherwise, on a rising clock edge (CLK 0 -> 1) with en high, Q latches D.
    - Power-up contents are undefined (a register holds nothing meaningful until
      cleared or loaded), so the value starts from a random bit pattern — assert
      CLR to establish a known 0. CLK, en, and CLR are ordinary inputs; only
      this propagate() logic gives them their meaning.
    """
    D = 'D'
    en = 'en'
    CLR = 'CLR'
    Q = 'Q'
    kind = 'RegisterClr'

    def __init__(self, js_id='', label='', bits=32):
        super().__init__(
            kind=RegisterClr.kind,
            js_id=js_id,
            label=label,
            bits=bits,
            named_inputs=[RegisterClr.D, RegisterClr.CLK,
                          RegisterClr.en, RegisterClr.CLR],
            named_outputs=[RegisterClr.Q])
        self.value = random.getrandbits(bits)  # initial state is random

    def propagate(self, output_name='Q', value=0):
        clr = self.safe_read_input(RegisterClr.CLR, bits=1)
        _, rising = self.clock_edge()
        en = self.safe_read_input(RegisterClr.en, bits=1)
        if clr:
            self.value = 0                       # asynchronous, dominant
        elif rising and en:
            self.value = self.safe_read_input(RegisterClr.D)
        return super().propagate(output_name=output_name, value=self.value)

    # Nothing special to do for clone(). BitsNode.clone() is enough.


class Addressable(BitsNode):
    """
    Addressable is for code shared between ROM and RAM
    """
    A = 'A'      # Address input
    D = 'D'      # Data output

    def __init__(self, kind, label='', js_id='', address_bits=4, data_bits=8, named_inputs=[], named_outputs=[]):
        named_inputs.append(Addressable.A)
        named_outputs.append(Addressable.D)
        super().__init__(kind=kind, label=label, js_id=js_id, bits=data_bits,
                         named_inputs=named_inputs, named_outputs=named_outputs)
        self.address_bits = address_bits
        self.data_bits = data_bits
        self.total_cells = 2 ** address_bits
        self.max_value = (2 ** data_bits) - 1
        # Zero-initialize directly, not via write_address: that emits a 'memory' event per
        # cell, which for a ROM (read-only, never writes at run time) floods the UI with
        # spurious updates the frontend then rejects as "memory update for non-RAM component".
        self.memory = [0] * self.total_cells


    def calc_address(self):
        addr = self.safe_read_input(Addressable.A, bits=self.address_bits)
        if addr >= self.total_cells:
            # Wrap around
            logger.warning(
                f'Addressable address {addr} wraps {self.address_bits} bits')
            addr = addr % self.total_cells
        return addr

    def read_address(self, addr=None):
        if addr is None:
            addr = self.calc_address()
        val = self.memory[addr]
        return val

    def write_address(self, addr=None, val=0):
        if addr is None:
            addr = self.calc_address()
        if val > self.max_value:
            logger.warning(
                f'Addressable value {val} overflows {self.data_bits} bits')
            val &= self.mask()
        self.memory[addr] = val
        # value as a string so a 64-bit cell survives the JS JSON round-trip exactly (BigInt on the
        # UI side); address stays an int (<= 16-bit address space).
        callbacks.emit('memory', self.js_id, {'address': addr, 'value': str(val)})


class ROM(Addressable):
    kind = 'ROM'
    sel = 'sel'  # Select/enable input

    def __init__(self, js_id='', address_bits=4, data_bits=8, data=None, label=''):
        super().__init__(
            kind=ROM.kind,
            js_id=js_id,
            label=label,
            named_inputs=[ROM.sel],
            named_outputs=[],
            address_bits=address_bits,
            data_bits=data_bits)
        # Initialize memory with provided data or zeros
        self.load_data(data)

    def propagate(self, output_name='D', value=0):
        # Get inputs
        selected = self.safe_read_input(ROM.sel, bits=1)
        val = self.read_address()
        if not selected:
            val = 0
        return super().propagate(output_name=output_name, value=val)

    def load_data(self, data):
        """
        This is redundant with having the data in the constructor, but
        I imagined that with a larger list of ROM elements, it would be
        unwieldy to have the whole list on the constructor line, so both
        syntaxes are accepted.
        """
        if data:
            for i in range(len(data)):
                if i < self.total_cells:
                    self.memory[i] = data[i]


class RAM(Clockable, Addressable):
    kind = 'RAM'
    Din = 'Din'
    ld = 'ld'
    st = 'st'

    def __init__(self, label='', js_id='', address_bits=4, data_bits=8):
        super().__init__(kind=RAM.kind, label=label, js_id=js_id,
                         named_inputs=[RAM.Din, RAM.ld, RAM.st, RAM.CLK],
                         named_outputs=[], address_bits=address_bits, data_bits=data_bits)

    def propagate(self, output_name=Addressable.D, value=0, bits=None):
        # Write on the rising CLK edge, not on level (see Clockable), otherwise
        # a cyclic read-modify-write datapath would never settle
        clk, rising = self.clock_edge()
        if clk == 0:
            return []
        addr = self.calc_address()

        st = self.safe_read_input(RAM.st, bits=1)
        if rising and st == 1:
            din = self.safe_read_input(RAM.Din)
            self.write_address(addr, din)

        ld = self.safe_read_input(RAM.ld, bits=1)
        if ld == 1:
            value = self.read_address(addr)
            return super().propagate(output_name=output_name, value=value)
        else:
            # Digital propagates 'Z' when when 'ld' is low
            return []
