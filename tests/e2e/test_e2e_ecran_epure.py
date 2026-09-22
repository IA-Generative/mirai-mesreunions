"""Enveloppe pytest du banc de l'écran épuré (`banc-ecran-epure.py`).

Le banc joue le vrai gabarit, le vrai bundle et le vrai menu commun dans un
navigateur (Playwright), routes bouchonnées. Sauté si Playwright n'est pas
là, si le bundle n'a pas été construit, ou si le dépôt frère mirai-apps-menu
n'est pas à côté (le banc lit son `src/menu.js`).
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

RACINE = pathlib.Path(__file__).resolve().parent.parent.parent
BANC = pathlib.Path(__file__).with_name("banc-ecran-epure.py")
BUNDLE = RACINE / "services" / "mesreunions-web" / "app" / "static" / "dist" / "shell.js"
MENU_JS = pathlib.Path(os.environ.get("MENU_JS", str(RACINE.parent / "mirai-apps-menu" / "src" / "menu.js")))


def _playwright_present() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _playwright_present(), reason="playwright (python) absent")
@pytest.mark.skipif(not BUNDLE.is_file(), reason="bundle Vite absent : npm run build")
@pytest.mark.skipif(not MENU_JS.is_file(), reason="mirai-apps-menu/src/menu.js introuvable (MENU_JS=…)")
def test_banc_ecran_epure():
    proc = subprocess.run([sys.executable, str(BANC)], capture_output=True, text=True, cwd=str(RACINE), timeout=300)
    if proc.returncode != 0:
        pytest.fail("banc en échec :\n" + proc.stdout[-4000:] + "\n" + proc.stderr[-2000:])
    assert "tout passe" in proc.stdout
