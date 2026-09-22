# GGL (Golden Gates Language)

[![Tests](https://github.com/usfca-cs-tools/ggl/actions/workflows/test.yml/badge.svg)](https://github.com/usfca-cs-tools/ggl/actions/workflows/test.yml)

The core Python engine for the Golden Gates digital logic circuit simulator.

This package is consumed as a git submodule by other repositories (for example
[`golden-gates`](https://github.com/usfca-cs-tools/golden-gates), whose Vue app
runs this engine in the browser via Pyodide) so the simulation logic has a
single source of truth.

## Layout

```
ggl/
├── pyproject.toml      # installable package metadata (src layout)
├── src/ggl/            # the engine package
│   ├── __init__.py     # package exports
│   ├── circuit.py      # Circuit: manages nodes and connections
│   ├── node.py         # base Node and Connector
│   ├── edge.py         # Edge connections
│   ├── io.py           # Input/Output/Clock nodes
│   ├── logic.py        # logic gates (AND, OR, NOT, NAND, NOR, XOR, ...)
│   ├── arithmetic.py   # arithmetic components
│   ├── plexers.py      # multiplexers / decoders
│   ├── memory.py       # registers / ROM / RAM
│   ├── wires.py        # splitters / mergers / tunnels
│   ├── component.py    # hierarchical components
│   ├── errors.py       # CircuitError for surfacing errors to front-ends
│   └── ggl_logging.py  # logging helpers (propagate to browser console)
└── tests/ggl/          # self-checking circuit programs
```

## Install

```sh
pip install -e .
```

This makes the package importable as `ggl`:

```python
from ggl import circuit, io, logic

c = circuit.Circuit()
g = logic.And(bits=1, label='r')
a = io.Input(bits=1, label='a'); a.value = 1
b = io.Input(bits=1, label='b'); b.value = 1
c.connect(a, g.input("0"))
c.connect(b, g.input("1"))
r = io.Output(bits=1, label='r'); c.connect(g, r)
c.run(); c.stop()
print(r.value)  # 1
```

## Coding style

We use `pycodestyle` / `autopep8`:

```sh
pip install pycodestyle autopep8
autopep8 --in-place src/ggl/*.py
```

## Callbacks (browser integration)

When running under Pyodide, the engine calls `window.__vueUpdateCallback` with
three scalar arguments:

```js
window.__vueUpdateCallback = (eventType, componentId, value) => { ... }
```

- `eventType` — `'value'`, `'step'`, or `'error'`
- `componentId` — the front-end component id (the `js_id` parameter)
- `value` — scalar or object payload depending on the event type

```python
# value updates
updateCallback('value', component_js_id, new_value)
# step highlighting
updateCallback('step', component_js_id, {'active': True, 'style': 'processing'})
```

## Logging

The default log level is `logging.WARN`. Logger objects propagate to the browser
console under Pyodide. To change the level in a source file:

```python
import logging
logger = new_logger(__name__, logging.INFO)
```

Outside Pyodide you can set the level via an environment variable instead of
editing code:

```sh
export ggloglevel='logging.INFO'
```

## Errors

1. Use the `Exception` class in `errors.py` to bubble errors up to the front-end.
2. Errors do not use the Vue callback mechanism.
3. Error strings are stored in the front-end locale file(s) and referred to by
   string IDs shared between the front-end and GGL.

## Tests

`tests/ggl/` contains circuit programs, each of which builds a circuit and
asserts its own expected values. `tests/test_circuits.py` runs every program in
a subprocess, so a failed assertion fails that case.

Run the suite with pytest:

```sh
uv run pytest
```

## Benchmarks

`scripts/benchmark.py` characterizes the engine's **effective clock frequency** — how
fast GGL can actually advance a circuit (the ceiling on Run and Run Tests), distinct from
the UI clock frequency you set on a circuit, which only paces the free-running `Run`. It
sweeps a synthetic `counter-bank ×N` circuit and reports settle passes per edge and
milliseconds per clock edge:

```sh
PYTHONPATH=src python3 scripts/benchmark.py
```

The Golden Gates app runs this same engine inside Pyodide (WASM). The app repo's
`web/scripts/benchmark-pyodide.mjs` runs these identical circuits both natively and under
that Pyodide, writing the combined table to `web/BENCHMARKS.md` — so you can see the
browser's overhead (empirically a flat ~1.5–1.8× on these circuits) alongside the native
numbers.
