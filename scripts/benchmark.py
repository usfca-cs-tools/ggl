"""Characterize GGL's effective clock frequency on synthetic, scalable circuits.

One "iteration" of ``Circuit.benchmark()`` is a single clock edge: toggle the clock
and settle the combinational logic -- exactly the work the free-running clock does per
edge, with no real-time pacing. A register latches on the RISING edge, so a full clock
CYCLE (one state update) is two iterations; the effective clock below is reported in
those full cycles per second.

The circuits are built in code (no .ggc files, no gg-solutions dependency) so the table
is reproducible and reviewable. The SAME builders run under Pyodide via
``web/scripts/benchmark-pyodide.mjs`` (it imports this module inside Pyodide and calls
``run_benchmarks``), so the two columns compare the identical circuit on the native
interpreter vs. the browser's WASM runtime.
"""

import json

from ggl import circuit, io, memory, arithmetic


def build_counter_bank(n, bits=32):
    """``n`` independent ``bits``-wide counters sharing one clock: each is a Register whose
    D input is Adder(Q, 1), so every rising edge advances all ``n`` counters. Node count
    scales ~2n (a register + an adder each); the shared clock/constants are counted once.
    A clocked circuit is the right vehicle -- toggling the clock forces real settling every
    iteration, unlike a static combinational circuit that re-settles to the same values."""
    c = circuit.Circuit()
    clk = io.Clock(label="CLK", frequency=1)      # frequency is ignored here; benchmark() toggles directly
    one = io.Constant(label="one", bits=bits); one.value = 1
    cin = io.Constant(label="cin", bits=1); cin.value = 0
    en = io.Constant(label="en", bits=1); en.value = 1
    for i in range(n):
        reg = memory.Register(label=f"r{i}", bits=bits, js_id=f"r{i}")
        add = arithmetic.Adder(label=f"a{i}", bits=bits, js_id=f"a{i}")
        c.connect(reg.output("Q"), add.input("a"))
        c.connect(one, add.input("b"))
        c.connect(cin, add.input("cin"))
        c.connect(add.output("sum"), reg.input("D"))
        c.connect(clk, reg.input("CLK"))
        c.connect(en, reg.input("en"))
    return c


# The synthetic sweep: a counter bank at growing sizes, to trace effective clock vs. size.
CIRCUITS = [("counter-bank x%d" % n, lambda n=n: build_counter_bank(n)) for n in (1, 4, 16, 64, 256)]


def _iterations_for(nodes):
    """Fewer edges on bigger circuits so each measurement stays a second or two but still
    averages over enough edges to be stable."""
    return max(200, min(4000, 200000 // max(nodes, 1)))


def run_benchmarks():
    """Build each circuit, prime it once, and time Circuit.benchmark(). Returns a list of
    dicts: label, deep_nodes, avg_passes (settle sweeps per edge), and per_edge_ms."""
    results = []
    for label, build in CIRCUITS:
        c = build()
        c.preflight()
        c.settle()  # prime: reach a stable state before timing
        nodes = c._deep_node_count()
        b = c.benchmark(_iterations_for(nodes))
        results.append({
            "label": label,
            "deep_nodes": b["deep_nodes"],
            "avg_passes": round(b["avg_passes"], 2),
            "per_edge_ms": b["per_iter_ms"],
            "iterations": b["iterations"],
        })
    return results


if __name__ == "__main__":
    print(json.dumps(run_benchmarks(), indent=2))
