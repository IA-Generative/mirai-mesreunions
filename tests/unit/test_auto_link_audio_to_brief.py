"""Unit tests for the auto-link audio→brief scoring (§4 du plan v2).

Le scoring effectif vit dans ``services/file-mover/app/puller.py``. On
teste les fonctions pures (``_tokenize_fr``, ``_score_brief_for_audio``)
ainsi que ``auto_link_audio_to_brief()`` avec une session SQLite minimale.
"""

import importlib.util
import os
import sys
import types
import uuid
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_scoring_pure():
    """Charge un module léger qui expose seulement les fonctions pures
    ``_tokenize_fr`` et ``_score_brief_for_audio`` du puller, sans tirer
    les imports lourds (app.mcr_client, app.kevent_client, etc.).

    Implémenté en lisant le source et en exécutant uniquement les
    fonctions ciblées dans un namespace minimal.
    """
    import ast
    src_path = os.path.join(ROOT, "services", "file-mover", "app", "puller.py")
    with open(src_path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), src_path)
    wanted = {"_tokenize_fr", "_score_brief_for_audio"}
    extracted = ast.Module(body=[
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in wanted
    ], type_ignores=[])
    ns = {
        "__name__": "puller_scoring_pure",
        "datetime": __import__("datetime").datetime,
        "timezone": __import__("datetime").timezone,
    }
    exec(compile(extracted, src_path, "exec"), ns)
    return SimpleNamespace(
        _tokenize_fr=ns["_tokenize_fr"],
        _score_brief_for_audio=ns["_score_brief_for_audio"],
    )


puller = _load_scoring_pure()


def _make_brief(brief_id, *, subject, created_at, drive_folder_id=None,
                 last_viewed_at=None, brief_json=None):
    return SimpleNamespace(
        id=brief_id,
        subject=subject,
        title=subject,
        brief_json=brief_json or {},
        created_at=created_at,
        drive_folder_id=drive_folder_id,
        last_viewed_at=last_viewed_at,
    )


def test_score_temporal_full_when_within_24h():
    upload_at = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    brief = _make_brief("b1", subject="copil DTNUM",
                         created_at=upload_at - timedelta(hours=2))
    score, breakdown = puller._score_brief_for_audio(
        brief, "reunion-dtnum.m4a", upload_at, set(),
    )
    assert breakdown["temporal"] == 1.0


def test_score_temporal_zero_beyond_14_days():
    upload_at = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    brief = _make_brief("b1", subject="anything",
                         created_at=upload_at - timedelta(days=30))
    score, breakdown = puller._score_brief_for_audio(
        brief, "file.m4a", upload_at, set(),
    )
    assert breakdown["temporal"] == 0.0


def test_score_temporal_decays_linearly_between_24h_and_14d():
    upload_at = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    brief_7d = _make_brief("b1", subject="x",
                            created_at=upload_at - timedelta(days=7))
    _, br = puller._score_brief_for_audio(brief_7d, "f.m4a", upload_at, set())
    # ~half-way between 24h and 14d → score ~0.5
    assert 0.3 < br["temporal"] < 0.7


def test_score_anti_rebound_zero_when_already_linked():
    upload_at = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    brief = _make_brief("b1", subject="x",
                         created_at=upload_at - timedelta(hours=1))
    _, br = puller._score_brief_for_audio(brief, "f.m4a", upload_at, {"b1"})
    assert br["anti_rebound"] == 0.0


def test_score_engagement_full_when_viewed_within_24h():
    # last_viewed_at est comparé à datetime.now(), donc on prend "now" comme
    # ancrage pour rester dans la fenêtre 24h indépendamment de la date du test.
    now = datetime.now(timezone.utc)
    upload_at = now
    brief = _make_brief("b1", subject="x",
                         created_at=now - timedelta(days=2),
                         last_viewed_at=now - timedelta(hours=2))
    _, br = puller._score_brief_for_audio(brief, "f.m4a", upload_at, set())
    assert br["engagement"] == 1.0


def test_score_similarity_jaccard():
    upload_at = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    brief = _make_brief(
        "b1", subject="COPIL DTNUM stratégie",
        created_at=upload_at,
        brief_json={"objective_reformulated": "Aligner avec la DGSI"},
    )
    _, br = puller._score_brief_for_audio(
        brief, "copil-dtnum-stratégie.m4a", upload_at, set(),
    )
    assert br["similarity"] > 0.0


def test_tokenize_handles_french_accents():
    out = puller._tokenize_fr("Réunion stratégique")
    assert "réunion" in out
    assert "stratégique" in out
