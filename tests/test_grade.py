"""The headless grading CLI: load a .ggc, inject/run a Test via ggl.view, print PASS/FAIL."""

import json
import os

from ggl import grade

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "ggc")


def _counter_test_props(stop_value, expected):
    """Instructor Test spec (Test component props): reset via CLR, EN=1, cycle until COUNT
    reaches stop_value, then check COUNT == expected."""
    return {
        "label": "count",
        "table": {"inputNames": ["EN"], "outputNames": ["COUNT"], "rows": [[1, expected]]},
        "reset_enabled": True, "reset_input_name": "CLR",
        "stop_enabled": True, "stop_output_name": "COUNT", "stop_output_value": stop_value,
    }


def _counter():
    with open(os.path.join(FIXTURE_DIR, "counter_4_bit.ggc")) as f:
        return json.load(f)


def test_inject_replaces_embedded_test_and_grades():
    ggc = grade.inject_test(_counter(), _counter_test_props(5, 5))
    ok, msg = grade.grade(ggc)
    assert ok and msg == "PASS"


def test_injected_test_can_fail():
    ggc = grade.inject_test(_counter(), _counter_test_props(5, 6))  # stops at 5, expects 6
    ok, msg = grade.grade(ggc)
    assert not ok and msg.startswith("FAIL:") and "testFailed" in msg and "got 5" in msg


def test_inject_drops_a_student_supplied_test():
    # A student can't sneak in a passing Test: injection removes any existing Test first.
    student = _counter()
    student["components"].append({"id": "cheat", "type": "test", "ports": [], "props": {
        "label": "cheat", "table": {"inputNames": [], "outputNames": [], "rows": []}}})
    ggc = grade.inject_test(student, _counter_test_props(5, 5))
    assert sum(1 for c in ggc["components"] if c["type"] == "test") == 1
    assert grade.grade(ggc)[0] is True


def test_grade_no_tests_is_not_a_pass():
    ok, msg = grade.grade(_counter())
    assert not ok and msg == "NO TESTS"


def test_cli_injects_test_and_prints_pass(tmp_path, capsys):
    circuit = tmp_path / "student.ggc"
    circuit.write_text(json.dumps(_counter()))
    spec = tmp_path / "counter.test.json"
    spec.write_text(json.dumps(_counter_test_props(3, 3)))
    rc = grade.main([str(circuit), "--test", str(spec)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "PASS"


def test_cli_fail_exits_nonzero(tmp_path, capsys):
    circuit = tmp_path / "student.ggc"
    circuit.write_text(json.dumps(_counter()))
    spec = tmp_path / "counter.test.json"
    spec.write_text(json.dumps(_counter_test_props(3, 9)))
    rc = grade.main([str(circuit), "--test", str(spec)])
    assert rc == 1
    assert capsys.readouterr().out.strip().startswith("FAIL:")
