"""Wrapper pytest qui exécute les tests JS du helper format.js (TKT-103).

On délègue à ``node --test`` (Node 18+). Si Node n'est pas dispo dans le
PATH (CI minimaliste), on skip — au même titre que les autres tests JS
de la suite (cf. test_mesreunions_web_template.py).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS_TEST_PATH = os.path.join(ROOT, "tests", "unit", "test_format_utils.js")


def _has_node() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _has_node(), reason="node binary required to run JS test runner")
def test_format_utils_js():
    """Exécute le test runner natif Node sur le helper format.js."""
    assert os.path.exists(JS_TEST_PATH), JS_TEST_PATH
    proc = subprocess.run(
        ["node", "--test", JS_TEST_PATH],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )
    if proc.returncode != 0:
        pytest.fail(
            "node --test failed (exit=%d)\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (proc.returncode, proc.stdout, proc.stderr)
        )
