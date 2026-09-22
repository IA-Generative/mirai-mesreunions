"""Aller-retour `_reparse_speaker_tagged_blocks` → `_rebuild_speaker_tagged`.

Ces deux fonctions encadrent TOUTE édition serveur de la transcription
(PATCH /hidden-blocks, POST /delete-hidden-blocks, POST /re-filter) : ce qui
n'est pas reconnu au parsing est réécrit hors de la base au rebuild.

Le parseur ignorait toute ligne non préfixée `>`. Or le formateur des imports
sous-titres écrivait un seul `> ` devant un texte pouvant contenir un retour à
la ligne interne (cue YouTube sur deux lignes). Conséquence mesurée en prod :
21 937 caractères en ligne de continuation, invisibles à l'affichage — et que
le premier « barrer un bloc » aurait effacés DÉFINITIVEMENT.
"""

import os
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def _puller():
    """Charge puller.py à l'exécution, JAMAIS à l'import.

    `_install_stubs` remplace `libs.shared.app.*` dans `sys.modules` : appelé au
    niveau module, il s'exécuterait pendant la COLLECTE pytest et les doublures
    fuiteraient dans tous les autres modules de test, y compris ceux qui tournent
    avant celui-ci. Une fixture repousse l'installation au moment où ce module
    s'exécute — même convention que test_puller_perform_pull.py.

    L'échafaudage est réutilisé plutôt que dupliqué (≈100 lignes de doublures).
    """
    from test_puller_perform_pull import _install_stubs, _load_puller

    _install_stubs()
    return _load_puller()


_WITH_CONTINUATION = (
    "**Intervenant_01** _(0:00.28 → 0:04.08)_\n"
    "> Le marché de l'emploi a cessé de savoir\n"
    "nommer ce qu'il achète.\n"
    "\n"
    "**Intervenant_01** _(0:04.08 → 0:06.06)_\n"
    "> Le titre n'est plus qu'un paravent.\n"
)


def test_reparse_attaches_continuation_lines_to_the_open_block(_puller):
    blocks = _puller._reparse_speaker_tagged_blocks(_WITH_CONTINUATION)
    assert len(blocks) == 2
    body = " ".join(ln.lstrip(">").strip() for ln in blocks[0]["body_lines"])
    assert body == "Le marché de l'emploi a cessé de savoir nommer ce qu'il achète."


def test_rebuild_does_not_lose_the_continuation_line(_puller):
    """Le scénario qui détruisait la donnée : parse puis réécriture en base."""
    blocks = _puller._reparse_speaker_tagged_blocks(_WITH_CONTINUATION)
    rebuilt = _puller._rebuild_speaker_tagged(blocks)
    assert "nommer ce qu'il achète." in rebuilt
    # Et la réécriture soigne la fiche : plus une seule ligne orpheline.
    orphans = [ln for ln in rebuilt.split("\n")
               if ln.strip() and not ln.startswith(">") and not ln.startswith("**")]
    assert orphans == []


def test_rebuild_is_stable_on_a_second_pass(_puller):
    once = _puller._rebuild_speaker_tagged(
        _puller._reparse_speaker_tagged_blocks(_WITH_CONTINUATION))
    twice = _puller._rebuild_speaker_tagged(
        _puller._reparse_speaker_tagged_blocks(once))
    assert once == twice


def test_reparse_still_ignores_blank_separator_lines(_puller):
    blocks = _puller._reparse_speaker_tagged_blocks(_WITH_CONTINUATION)
    assert all("" not in [ln.strip() for ln in b["body_lines"]] for b in blocks)


def test_reparse_preserves_canonical_blocks_byte_for_byte(_puller):
    """Non-régression : un texte déjà bien formé doit ressortir identique."""
    canonical = (
        "**Intervenant_01** _(0:00.00 → 0:03.00)_\n"
        "> Une phrase propre.\n"
        "\n"
        "**Intervenant_02** _(0:03.00 → 0:07.50)_\n"
        "> Une autre phrase propre.\n"
    )
    rebuilt = _puller._rebuild_speaker_tagged(
        _puller._reparse_speaker_tagged_blocks(canonical))
    assert rebuilt == canonical
