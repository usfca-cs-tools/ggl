"""Headless grading entry point.

Loads a Golden Gates circuit (a ``.ggc`` JSON file), runs a ``Test`` against it through
``ggl.view`` (mode="test") in plain CPython — no browser, no Pyodide — and prints a single
result line to stdout for the autograder to compare against an expected value:

    PASS                      all Tests passed
    FAIL: <detail>            a Test's expected output didn't match, or the circuit
                              errored (e.g. an unconnected input)
    NO TESTS                  no Test to grade

Test provenance is INSTRUCTOR-INJECTED: the grading Test lives in a separate JSON spec that
the instructor controls (in the project's tests dir), and ``--test`` merges it onto the
student's circuit, replacing any Test the student may have embedded. The student just has to
match the interface labels the assignment specifies (the Test references Inputs/Outputs by
label). Without ``--test`` it grades whatever Test is already in the circuit.

Usage:  python -m ggl.grade <circuit.ggc> [--test <test.json>]

The autograder compares stdout line-by-line (case-insensitive), so a project's test case
sets ``expected = "PASS"`` and runs
``input = ["python3", "-m", "ggl.grade", "<circuit>", "--test", "$project_tests/<t>.json"]``.
"""

import argparse
import json
import sys

from . import view


def inject_test(ggc, test_props):
    """Return a copy of the circuit with the instructor's Test as its only Test.

    ``test_props`` is the Test component's props (label, table={inputNames, outputNames,
    rows}, and optional stop_*/reset_*). Any Test already in the circuit is dropped so a
    student can't substitute a passing one.
    """
    components = [c for c in (ggc.get("components") or []) if c.get("type") != "test"]
    components.append({"id": "__injected_test__", "type": "test", "ports": [], "props": test_props})
    return {**ggc, "components": components}


def grade(ggc):
    """Grade a .ggc dict. Returns (ok: bool, message: str).

    Builds the circuit and its Test via ggl.view and evaluates it (mode="test", which emits
    ``<test>.evaluate(circuit0)`` — for a clocked Test it resets then cycles the clock until
    the stop output is reached). A failed expectation or circuit error is raised by the engine
    as a CircuitError, whose structured detail we format for feedback.
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
    parser = argparse.ArgumentParser(
        prog="python -m ggl.grade",
        description="Grade a Golden Gates .ggc circuit against a Test.")
    parser.add_argument("circuit", help="path to the circuit .ggc")
    parser.add_argument("--test", dest="test", metavar="TEST.json", default=None,
                        help="instructor Test spec (JSON of the Test's props); replaces any "
                             "Test in the circuit")
    args = parser.parse_args(argv)

    with open(args.circuit) as f:
        ggc = json.load(f)
    if args.test:
        with open(args.test) as f:
            ggc = inject_test(ggc, json.load(f))

    ok, message = grade(ggc)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
