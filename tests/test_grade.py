"""The headless grading CLI: load a .ggc, run its Tests via ggl.view, print PASS/FAIL."""

import json
import os

from ggl import grade

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "ggc")


def _counter_with_test(stop_value, expected):
    """The real 4-bit counter fixture plus a clocked Test: reset via CLR, EN=1, cycle
    until COUNT reaches stop_value, then check COUNT == expected."""
    with open(os.path.join(FIXTURE_DIR, "counter_4_bit.ggc")) as f:
        ggc = json.load(f)
    ggc["components"].append({
        "id": "T", "type": "test", "x": 0, "y": 0, "ports": [],
        "props": {
            "label": "count",
            "table": {"inputNames": ["EN"], "outputNames": ["COUNT"], "rows": [[1, expected]]},
            "reset_enabled": True, "reset_input_name": "CLR",
            "stop_enabled": True, "stop_output_name": "COUNT", "stop_output_value": stop_value,
        },
    })
    return ggc


def test_grade_passing_sequential():
    ok, msg = grade.grade(_counter_with_test(5, 5))
    assert ok and msg == "PASS"


def test_grade_failing_sequential():
    ok, msg = grade.grade(_counter_with_test(5, 6))  # stops at COUNT=5, expects 6
    assert not ok
    assert msg.startswith("FAIL:") and "testFailed" in msg and "got 5" in msg


def test_grade_no_tests_is_not_a_pass():
    with open(os.path.join(FIXTURE_DIR, "counter_4_bit.ggc")) as f:
        ggc = json.load(f)
    ok, msg = grade.grade(ggc)
    assert not ok and msg == "NO TESTS"


def test_cli_main_prints_pass_and_exits_zero(tmp_path, capsys):
    p = tmp_path / "c.ggc"
    p.write_text(json.dumps(_counter_with_test(3, 3)))
    rc = grade.main(["ggl.grade", str(p)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "PASS"


def test_cli_main_fail_exits_nonzero(tmp_path, capsys):
    p = tmp_path / "c.ggc"
    p.write_text(json.dumps(_counter_with_test(3, 9)))
    rc = grade.main(["ggl.grade", str(p)])
    assert rc == 1
    assert capsys.readouterr().out.strip().startswith("FAIL:")
