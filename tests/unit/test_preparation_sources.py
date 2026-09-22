"""Tests du contrat ``sources[]`` (parsing, plafonds, assainissement).

Le payload vient du navigateur : ces tests couvrent autant la forme nominale
que les entrées hostiles, puisque le contenu accepté ici finit dans le prompt
envoyé au modèle.
"""

import importlib.util
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
WEB = os.path.join(ROOT, "services", "mesreunions-web")
if WEB not in sys.path:
    sys.path.insert(0, WEB)


def _load_sources():
    path = os.path.join(WEB, "app", "modules", "preparations", "sources.py")
    spec = importlib.util.spec_from_file_location("prep_sources_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


src = _load_sources()


# ─── Rétro-compatibilité du champ historique ──────────────────────


def test_drive_folder_alone_is_preserved():
    """Le chemin d'origine (URL collée, pas de sources[]) reste intact."""
    out = src.parse_sources(None, drive_folder_id="abc-123", drive_folder_host="drive.test")
    assert out == [{"type": "drive_folder", "id": "abc-123",
                    "host": "drive.test", "label": ""}]


def test_drive_folder_is_deduplicated_against_sources():
    """Coller une URL ET cocher le même dossier ne l'ingère pas deux fois."""
    out = src.parse_sources(
        [{"type": "drive_folder", "id": "abc-123", "host": "drive.test"}],
        drive_folder_id="abc-123", drive_folder_host="drive.test",
    )
    assert len(out) == 1


def test_no_sources_at_all_is_valid():
    assert src.parse_sources(None) == []


# ─── Validation ───────────────────────────────────────────────────


def test_sources_must_be_a_list():
    with pytest.raises(src.SourceError):
        src.parse_sources({"type": "inline"})


def test_unknown_type_is_rejected():
    with pytest.raises(src.SourceError, match="Type de source inconnu"):
        src.parse_sources([{"type": "ftp"}])


@pytest.mark.parametrize("bad_id", ["", "   ", "../etc/passwd", "a/b", "x" * 300])
def test_path_like_ids_are_rejected(bad_id):
    """Un identifiant Drive est opaque : les formes « chemin » sont refusées."""
    with pytest.raises(src.SourceError):
        src.parse_sources([{"type": "drive_folder", "id": bad_id}])


def test_empty_inline_text_is_rejected():
    with pytest.raises(src.SourceError):
        src.parse_sources([{"type": "inline", "text": "   "}])


def test_drive_files_needs_at_least_one_item():
    with pytest.raises(src.SourceError):
        src.parse_sources([{"type": "drive_files", "items": []}])


# ─── Plafonds ─────────────────────────────────────────────────────


def test_too_many_sources_is_rejected():
    many = [{"type": "inline", "text": "x"} for _ in range(src.MAX_SOURCES + 1)]
    with pytest.raises(src.SourceError, match="Trop de sources"):
        src.parse_sources(many)


def test_too_many_drive_folders_is_rejected():
    many = [{"type": "drive_folder", "id": f"f{i}"} for i in range(src.MAX_DRIVE_FOLDERS + 1)]
    with pytest.raises(src.SourceError, match="Trop de dossiers"):
        src.parse_sources(many)


def test_too_many_preparations_is_rejected():
    many = [{"type": "preparation", "id": f"p{i}"} for i in range(src.MAX_PREPARATIONS + 1)]
    with pytest.raises(src.SourceError, match="Trop de réunions"):
        src.parse_sources(many)


def test_oversized_inline_text_is_truncated_not_rejected():
    out = src.parse_sources([{"type": "inline", "text": "a" * (src.MAX_INLINE_CHARS + 500)}])
    assert len(out[0]["text"]) == src.MAX_INLINE_CHARS
    assert out[0]["truncated"] is True


def test_inline_total_budget_is_enforced():
    entries = [
        {"type": "inline", "title": f"Mail {i}", "text": "a" * src.MAX_INLINE_CHARS}
        for i in range(4)
    ]
    with pytest.raises(src.SourceError, match="au total"):
        src.parse_sources(entries)


# ─── Assainissement ───────────────────────────────────────────────


def test_inline_text_is_sanitized_at_parse_time():
    """Le contenu hostile ne doit pas circuler dans le processus."""
    out = src.parse_sources([{"type": "inline", "text": "Bonjour​caché‮"}])
    assert "​" not in out[0]["text"]
    assert "‮" not in out[0]["text"]
    assert "Bonjour" in out[0]["text"]


def test_labels_are_capped():
    out = src.parse_sources([{"type": "drive_folder", "id": "ok", "label": "L" * 500}])
    assert len(out[0]["label"]) == src.MAX_LABEL_CHARS


# ─── Vue publique et pré-conditions ───────────────────────────────


def test_public_view_never_leaks_inline_content():
    """Le corps d'un mail n'a rien à faire dans un JSONB persisté."""
    parsed = src.parse_sources([{"type": "inline", "title": "Re: budget",
                                 "text": "SECRET-INTERNE"}])
    view = src.public_view(parsed)
    assert "SECRET-INTERNE" not in repr(view)
    assert view[0]["title"] == "Re: budget"
    assert view[0]["chars"] > 0


def test_has_drive_source_discriminates():
    assert src.has_drive_source(src.parse_sources([{"type": "drive_folder", "id": "a"}]))
    assert src.has_drive_source(
        src.parse_sources([{"type": "drive_files", "items": [{"id": "a"}]}]))
    # Une préparation nourrie de réunions passées et de mails n'a besoin
    # d'aucun jeton Drive : c'est ce que cette fonction permet de savoir.
    assert not src.has_drive_source(src.parse_sources([
        {"type": "preparation", "id": "p1"},
        {"type": "inline", "text": "bonjour"},
    ]))


def test_preparation_include_defaults_and_filters():
    out = src.parse_sources([{"type": "preparation", "id": "p1", "include": ["brief", "bogus"]}])
    assert out[0]["include"] == ["brief"]
    out = src.parse_sources([{"type": "preparation", "id": "p1"}])
    assert out[0]["include"] == ["brief", "key_points"]
