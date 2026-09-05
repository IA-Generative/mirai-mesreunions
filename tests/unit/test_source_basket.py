"""Wrapper pytest qui exécute les tests JS du panier de sources.

Même gabarit que ``test_format_utils.py`` : on délègue à ``node --test``
(Node 18+). Si Node n'est pas dans le PATH (CI minimaliste), on skip, comme
les autres tests JS de la suite.

Les fonctions couvertes (``toApiSources``, ``computeBudget``,
``isReadableFile``, ``mergeEntries``) portent deux contrats qu'aucun test
Python ne peut vérifier : le format exact envoyé à
``POST /api/preparations`` et la table des formats lisibles, miroir de
``services/dmz-to-internal-bridge/app/doc_extractor.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS_TEST_PATH = os.path.join(ROOT, "tests", "unit", "test_source_basket.js")


def _has_node() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _has_node(), reason="node binary required to run JS test runner")
def test_source_basket_js():
    """Exécute le test runner natif Node sur lib/source-basket.js."""
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
