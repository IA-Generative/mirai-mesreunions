"""Wrapper pytest qui exécute les tests JS de l'ancrage du toggle « Mode avancé ».

Même patron que ``test_format_utils.py`` : on délègue à ``node --test`` (Node 18+),
et on skip si Node n'est pas dans le PATH.

Le test qu'il porte mesure une GÉOMÉTRIE, pas une apparence — le toggle et la barre
flottante du menu commun se recouvraient, et là où ils se recouvraient le bouton
était visible et INERTE (le menu monte à z-index 2000). Une capture d'écran ne
montre pas ce défaut-là ; un rectangle, si.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS_TEST_PATH = os.path.join(ROOT, "tests", "unit", "test_advanced_toggle_anchor.js")


def _has_node() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _has_node(), reason="node binary required to run JS test runner")
def test_advanced_toggle_anchor_js():
    """Exécute le test runner natif Node sur lib/advanced-toggle-anchor.js."""
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
