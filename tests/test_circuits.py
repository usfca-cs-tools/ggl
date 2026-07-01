"""Run each circuit program in ``tests/ggl/`` as a self-checking test.

Every program builds a circuit and asserts its own expected values, so a failed
assertion exits non-zero and fails that case here. Each runs in a subprocess so
the programs stay isolated from each other.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent / "ggl"
SRC_DIR = Path(__file__).parent.parent / "src"

SCRIPTS = sorted(TESTS_DIR.glob("*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.stem)
def test_circuit(script):
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC_DIR)] + ([existing] if existing else [])
    )

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(TESTS_DIR),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"{script.stem} exited {result.returncode}\nstderr:\n{result.stderr}"
    )
