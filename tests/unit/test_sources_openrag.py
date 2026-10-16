"""Wrapper pytest qui exécute les tests JS de aplatirSource (widget RAG).

Même gabarit que ``test_source_basket.py`` : on délègue à ``node --test``
(Node 18+). Si Node n'est pas dans le PATH, on skip.

``aplatirSource`` ramène une source OpenRAG 2.3.1 (métadonnées sous
``chunk``) à la forme à plat de 2.2.1 avant que le widget en tire ses puces.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS_TEST_PATH = os.path.join(ROOT, "tests", "unit", "test_sources_openrag.js")


def _has_node() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _has_node(), reason="node binary required to run JS test runner")
def test_sources_openrag_js():
    """Exécute le test runner natif Node sur lib/sources-openrag.js."""
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
