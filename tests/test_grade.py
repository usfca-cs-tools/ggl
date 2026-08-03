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
    assert not ok and msg.startswith("FAIL:")
    # Student-readable: names the output and both values, no raw error code.
    assert 'output "COUNT"' in msg and "expected 6" in msg and "got 5" in msg
    assert "testFailed" not in msg


def test_inject_drops_a_student_supplied_test():
    # A student can't sneak in a passing Test: injection removes any existing Test first.
    student = _counter()
    student["components"].append({"id": "cheat", "type": "test", "ports": [], "props": {
        "label": "cheat", "table": {"inputNames": [], "outputNames": [], "rows": []}}})
    ggc = grade.inject_test(student, _counter_test_props(5, 5))
    assert sum(1 for c in ggc["components"] if c["type"] == "test") == 1
    assert grade.grade(ggc)[0] is True


def _no_reset_props(stop_value, expected):
    props = _counter_test_props(stop_value, expected)
    props.pop("reset_enabled")
    props.pop("reset_input_name")
    props["label"] = "noreset"
    return props


def test_clock_without_reset_is_flagged():
    with_reset = grade.inject_test(_counter(), _counter_test_props(5, 5))
    assert grade.clock_without_reset(with_reset) == []
    without_reset = grade.inject_test(_counter(), _no_reset_props(5, 5))
    assert grade.clock_without_reset(without_reset) == ["noreset"]


def test_cli_warning_goes_to_stderr_not_stdout(tmp_path, capsys):
    circuit = tmp_path / "student.ggc"
    circuit.write_text(json.dumps(_counter()))
    spec = tmp_path / "t.json"
    spec.write_text(json.dumps(_no_reset_props(5, 5)))
    grade.main([str(circuit), "--test", str(spec)])
    out = capsys.readouterr()
    assert "warning" in out.err.lower() and "reset" in out.err.lower()
    assert "warning" not in out.out.lower()  # the graded stdout stays clean


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


# --- Failure-mode messaging: each mode must produce one clear FAIL line on stdout, since
# that stdout is exactly what `grade test -v` shows the student in its expected-vs-actual diff.

def test_missing_circuit_file_is_a_clean_fail(tmp_path, capsys):
    rc = grade.main([str(tmp_path / "nope.ggc"), "--test", str(tmp_path / "t.json")])
    out = capsys.readouterr().out.strip()
    assert rc == 1 and out == f"FAIL: circuit file not found: {tmp_path / 'nope.ggc'}"


def test_missing_test_spec_is_a_clean_fail(tmp_path, capsys):
    circuit = tmp_path / "student.ggc"
    circuit.write_text(json.dumps(_counter()))
    rc = grade.main([str(circuit), "--test", str(tmp_path / "missing.json")])
    out = capsys.readouterr().out.strip()
    assert rc == 1 and out.startswith("FAIL: test spec not found:")


def test_corrupt_circuit_json_is_a_clean_fail(tmp_path, capsys):
    circuit = tmp_path / "student.ggc"
    circuit.write_text("{ not json")
    rc = grade.main([str(circuit)])
    out = capsys.readouterr().out.strip()
    assert rc == 1 and out.startswith("FAIL: circuit file is not valid JSON")


def test_mis_named_input_names_the_label_and_lists_available():
    bad = _counter_test_props(5, 5)
    bad["table"]["inputNames"] = ["NOPE"]
    ok, msg = grade.grade(grade.inject_test(_counter(), bad))
    assert not ok
    assert 'no input labeled "NOPE"' in msg
    assert "EN" in msg and "CLR" in msg  # tells the student what the circuit actually has


def test_mis_named_output_names_the_label_and_lists_available():
    bad = _counter_test_props(5, 5)
    bad["table"]["outputNames"] = ["WRONG"]
    ok, msg = grade.grade(grade.inject_test(_counter(), bad))
    assert not ok
    assert 'no output labeled "WRONG"' in msg
    assert "COUNT" in msg
