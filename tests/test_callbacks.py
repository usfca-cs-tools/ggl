"""The host registers an update callback with callbacks.set_callback(); a
settle then delivers one coalesced 'batch' call to it. Guards the register ->
emit contract so it can't silently break (as it did when the callback was
routed through a module-cached builtins reference)."""

import json

import pytest

from ggl import callbacks, circuit, io, logic


@pytest.fixture(autouse=True)
def clear_callback():
    callbacks.set_callback(None)
    yield
    callbacks.set_callback(None)


def test_settle_delivers_one_batch_to_registered_callback():
    calls = []
    callbacks.set_callback(lambda event, cid, payload: calls.append((event, cid, payload)))

    c = circuit.Circuit()
    a = io.Input(bits=1, label="a")
    a.value = 1
    g = logic.Not(bits=1)
    out = io.Output(bits=1, js_id="out_1")
    c.connect(a, g.input("0"))
    c.connect(g, out)
    c.run()

    assert len(calls) == 1
    event, cid, payload = calls[0]
    assert event == "batch"
    assert ["value", "out_1", 0] in json.loads(payload)


def test_no_callback_is_a_noop():
    # With nothing registered, running must not raise.
    c = circuit.Circuit()
    a = io.Input(bits=1)
    a.value = 1
    out = io.Output(bits=1, js_id="out_1")
    c.connect(a, out)
    c.run()


def test_callback_registered_after_import_is_still_seen():
    # Registration happens well after this module (and callbacks) import, which
    # is exactly the ordering that broke when a stale reference was cached.
    seen = []
    callbacks.set_callback(lambda *a: seen.append(a))
    callbacks.start_batch()
    callbacks.emit("value", "x", 1)
    callbacks.flush_batch()
    assert seen and seen[0][0] == "batch"
