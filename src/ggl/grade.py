"""Headless grading entry point.

Loads a Golden Gates circuit (a ``.ggc`` JSON file), builds and runs its ``Test``
components through ``ggl.view`` (mode="test") in plain CPython — no browser, no Pyodide —
and prints a single result line to stdout for the autograder to compare against an
expected value:

    PASS                      all Tests passed
    FAIL: <detail>            a Test's expected output didn't match, or the circuit
                              errored (e.g. an unconnected input)
    NO TESTS                  the circuit contains no Test component to grade

Usage:  python -m ggl.grade <circuit.ggc>

The line-based, case-insensitive comparison the autograder uses means a project's test
case sets ``expected = "PASS"`` and runs ``input = ["python3", "-m", "ggl.grade", "<file>"]``.
"""

import json
import sys

from . import view


def grade(ggc):
    """Grade a .ggc dict. Returns (ok: bool, message: str).

    Builds the circuit and its Tests via ggl.view and evaluates them (mode="test", which
    emits `<test>.evaluate(circuit0)` — for a clocked Test that resets then cycles the
    clock until the stop output is reached). A failed expectation or a circuit error is
    raised by the engine as a CircuitError, whose structured detail we format for feedback.
    """
    components = ggc.get("components", []) or []
    if not any(c.get("type") == "test" for c in components):
        return False, "NO TESTS"

    program = view.generate(ggc, mode="test")
    try:
        exec(compile(program, "<ggl.grade>", "exec"), {})  # noqa: S102 - our own code
    except Exception as err:  # noqa: BLE001 - any failure is a grading failure
        detail = getattr(err, "to_dict", None)
        if callable(detail):
            d = detail()
            parts = [str(d.get("error_code", "error"))]
            if d.get("component_label"):
                parts.append(f'component "{d["component_label"]}"')
            if d.get("output"):
                parts.append(f'output {d["output"]}')
            if "expected" in d:
                parts.append(f'expected {d["expected"]}')
            if "actual" in d:
                parts.append(f'got {d["actual"]}')
            return False, "FAIL: " + " ".join(parts)
        return False, f"FAIL: {err}"
    return True, "PASS"


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2:
        print("usage: python -m ggl.grade <circuit.ggc>")
        return 2
    with open(argv[1]) as f:
        ggc = json.load(f)
    ok, message = grade(ggc)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
