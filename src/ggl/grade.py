"""Headless grading entry point.

Loads a Golden Gates circuit (a ``.ggc`` JSON file), runs a ``Test`` against it through
``ggl.view`` (mode="test") in plain CPython — no browser, no Pyodide — and prints a single
result line to stdout for the autograder to compare against an expected value:

    PASS                      all Tests passed
    FAIL: <detail>            a Test's expected output didn't match, or the circuit
                              errored (e.g. an unconnected input)
    NO TESTS                  no Test to grade

Test provenance is INSTRUCTOR-INJECTED: the grading Test lives in a separate ``.ggc`` that the
instructor authors and exports from the app (a circuit whose only meaningful component is the
grading Test), and ``--test`` merges that Test onto the student's circuit, replacing any Test
the student may have embedded. The student just has to match the interface labels the
assignment specifies (the Test references Inputs/Outputs by label). Without ``--test`` it
grades whatever Test is already in the circuit.

Usage:  python -m ggl.grade <circuit.ggc> [--test <test.ggc>]

The autograder compares stdout line-by-line (case-insensitive), so a project's test case
sets ``expected = "PASS"`` and runs
``input = ["python3", "-m", "ggl.grade", "<circuit>", "--test", "$project_tests/<t>.json"]``.
"""

import argparse
import json
import sys

from . import view
from .project import ProjectError, load_project


def inject_test(ggc, test_props):
    """Return a copy of the circuit with the instructor's Test as its only Test.

    ``test_props`` is the Test component's props (label, table={inputNames, outputNames,
    rows}, and optional stop_*/reset_*). Any Test already in the circuit is dropped so a
    student can't substitute a passing one.
    """
    components = [c for c in (ggc.get("components") or []) if c.get("type") != "test"]
    components.append({"id": "__injected_test__", "type": "test", "ports": [], "props": test_props})
    return {**ggc, "components": components}


def _test_props_from_spec(spec, path):
    """A ``--test`` spec is a full ``.ggc`` containing exactly the grading Test (authored and
    exported from the app). Return that Test's props, or None (after printing a FAIL line) when
    the file has no Test or more than one — the same channel every other grading failure uses."""
    tests = [c for c in (spec.get("components") or []) if c.get("type") == "test"]
    if len(tests) != 1:
        which = "none" if not tests else f"{len(tests)}"
        print(f"FAIL: test spec must contain exactly one Test component (found {which}): {path}")
        return None
    return tests[0].get("props") or {}


def clock_without_reset(ggc):
    """Return the labels of Tests that grade a clocked circuit (has a Clock) without a
    reset pulse. Such a test cycles the clock from the circuit's power-on state, which for
    a Register is randomized — so the result can differ between runs, breaking the
    student/instructor "same result" guarantee. The fix is a reset (reset_enabled)."""
    components = ggc.get("components", []) or []
    if not any(c.get("type") == "clock" for c in components):
        return []
    labels = []
    for c in components:
        if c.get("type") == "test" and not (c.get("props", {}) or {}).get("reset_enabled"):
            labels.append((c.get("props", {}) or {}).get("label") or "test")
    return labels


def _format_failure(d):
    """Turn a CircuitError's structured detail into one student-readable ``FAIL:`` line.

    ``grade test -v`` shows the diff of expected-vs-actual *stdout*, so this line is the
    entire failure explanation a student sees. Each known failure mode gets a sentence that
    names the offending thing and, where useful, what the circuit actually offers. Unknown
    engine errors fall back to a cleaned-up code so nothing shows a raw i18n key.
    """
    code = d.get("error_code", "error")
    name = d.get("name")
    label = d.get("component_label")

    def avail(kind):
        labels = d.get("available") or []
        return f'circuit {kind}: ' + (", ".join(labels) if labels else "(none)")

    if code == "testInputNotFound":
        return f'FAIL: no input labeled "{name}" — {avail("inputs")}'
    if code == "testOutputNotFound":
        return f'FAIL: no output labeled "{name}" — {avail("outputs")}'
    if code == "testAmbiguousLabel":
        return f'FAIL: more than one component is labeled "{name}"'
    if code == "testNoClock":
        return "FAIL: this test drives a clock, but the circuit has no clock component"
    if code == "testStopNotReached":
        return (f'FAIL: output "{name}" never reached {d.get("value")} within '
                f'{d.get("cycles")} clock cycles')
    if code == "testFailed":
        msg = (f'FAIL: output "{d.get("output")}" expected {d.get("expected")} '
               f'but got {d.get("actual")}')
        if d.get("inputs"):
            msg += f' (inputs: {d["inputs"]})'
        return msg
    if code == "tunnelNoDriver":
        return f'FAIL: tunnel net "{d.get("label")}" has no driver'
    if code == "tunnelMultipleDrivers":
        return f'FAIL: tunnel net "{d.get("label")}" has more than one driver'
    # Any other engine error (open input, bit-width mismatch, short circuit, ...): strip the
    # "simulation.errors." i18n prefix so the student sees a plain code, plus context.
    plain = str(code).rsplit(".", 1)[-1]
    parts = [f"FAIL: {plain}"]
    if label:
        parts.append(f'in component "{label}"')
    if d.get("port_name"):
        parts.append(f'(port {d["port_name"]})')
    return " ".join(parts)


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

    try:
        # generate() can itself raise a CircuitError (e.g. an invalid tunnel net), so it's
        # inside the try — its structured detail is formatted like any run-time failure.
        program = view.generate(ggc, mode="test")
        exec(compile(program, "<ggl.grade>", "exec"), {})  # noqa: S102 - our own code
    except Exception as err:  # noqa: BLE001 - any failure is a grading failure
        detail = getattr(err, "to_dict", None)
        if callable(detail):
            return False, _format_failure(detail())
        return False, f"FAIL: {err}"
    return True, "PASS"


def _load_json(path, what):
    """Load a JSON file, or print a ``FAIL:`` line and return None. ``what`` names the file
    for the message (e.g. "circuit file", "test spec")."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"FAIL: {what} not found: {path}")
    except json.JSONDecodeError as err:
        print(f"FAIL: {what} is not valid JSON ({path}): {err}")
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m ggl.grade",
        description="Grade a Golden Gates .ggc circuit against a Test.")
    parser.add_argument("circuit", help="path to the circuit .ggc")
    parser.add_argument("--test", dest="test", metavar="TEST.ggc", default=None,
                        help="instructor Test spec (a .ggc containing the grading Test); "
                             "replaces any Test in the circuit")
    args = parser.parse_args(argv)

    # A student most often fails here: the circuit isn't committed, or is named something
    # other than what the assignment's test case expects. Say so on stdout (where `grade
    # test -v` shows it) instead of dumping a traceback to stderr. Loading is directory-aware,
    # like the app opening a project folder: subcircuits referenced by filename are inlined from
    # sibling .ggc files into a self-contained model.
    try:
        ggc = load_project(args.circuit)
    except FileNotFoundError:
        print(f"FAIL: circuit file not found: {args.circuit}")
        return 1
    except json.JSONDecodeError as err:
        print(f"FAIL: circuit file is not valid JSON ({args.circuit}): {err}")
        return 1
    except ProjectError as err:
        print(f"FAIL: {err}")
        return 1
    if args.test:
        spec = _load_json(args.test, "test spec")
        if spec is None:
            return 1
        test_props = _test_props_from_spec(spec, args.test)
        if test_props is None:
            return 1
        ggc = inject_test(ggc, test_props)

    # Diagnostic to stderr (kept off stdout so it never affects the graded result): a
    # clocked test with no reset can be non-deterministic, so warn the author.
    for label in clock_without_reset(ggc):
        print(f"warning: Test '{label}' cycles a clocked circuit with no reset pulse; "
              f"its result can depend on the power-on state — add reset_enabled",
              file=sys.stderr)

    ok, message = grade(ggc)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
