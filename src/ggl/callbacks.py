"""Front-end update callbacks, coalesced per settle().

Components report updates through ``builtins.updateCallback`` (installed by the
Pyodide host). Firing one call per node per relaxation pass floods the JS
boundary; instead settle() opens a batch, emit() accumulates, and one
``updateCallback('batch', None, updates)`` crosses at the end.

value/step updates collapse by js_id (last write wins — the settled value is all
the UI needs); memory writes are kept in order since a single component can
write several addresses in one settle.

With no updateCallback installed (headless), batching stays off and emit() is a
cheap no-op.
"""

import builtins
import json

from .ggl_logging import new_logger

logger = new_logger(__name__)

_scalar = None   # js_id -> (event, payload); None means "not batching"
_memory = None   # list of (js_id, payload)


def _sink():
    return getattr(builtins, "updateCallback", None)


def start_batch():
    global _scalar, _memory
    if _sink() is None:
        _scalar = _memory = None
        return
    _scalar = {}
    _memory = []


def emit(event, js_id, payload):
    if not js_id:
        return
    if _scalar is None:
        _fire(_sink(), event, js_id, payload)
    elif event == "memory":
        _memory.append((js_id, payload))
    else:
        _scalar[js_id] = (event, payload)


def flush_batch():
    global _scalar, _memory
    scalar, memory = _scalar, _memory
    _scalar = _memory = None
    if not scalar and not memory:
        return
    fn = _sink()
    if fn is None:
        return
    updates = [[event, jid, payload] for jid, (event, payload) in scalar.items()]
    updates += [["memory", jid, payload] for jid, payload in memory]
    # Pass a JSON string, not a nested list — proxies don't cross cleanly.
    try:
        fn("batch", None, json.dumps(updates))
    except Exception as e:
        logger.error(f"Batch callback failed: {e}")


def _fire(fn, event, js_id, payload):
    if fn is None:
        return
    try:
        fn(event, js_id, payload)
    except Exception as e:
        logger.error(f"Callback failed: {e}")
