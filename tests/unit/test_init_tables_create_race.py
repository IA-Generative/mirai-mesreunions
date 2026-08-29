"""init_tables — tolérance à la course create_all entre replicas.

Incident prod-bêta 2026-08-29 : deux pods mesreunions-web bootent en même
temps après le déploiement de web_session_tokens, les deux passent le
checkfirst, un seul CREATE TABLE gagne, le perdant meurt en
IntegrityError (UniqueViolation pg_type) → worker exit code 3 → restart.
Le retry doit convertir la course en boot légèrement plus lent.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("sqlalchemy")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy.exc import IntegrityError

from libs.shared.app import database as db_mod
from libs.shared.app.config import DatabaseConfig


def _integrity_error():
    return IntegrityError(
        "CREATE TABLE web_session_tokens (...)",
        {},
        Exception('duplicate key value violates unique constraint "pg_type_typname_nsp_index"'),
    )


def test_init_tables_retries_on_create_race():
    """1er essai : IntegrityError (course) ; 2e essai : succès silencieux."""
    calls = {"n": 0}

    def _create_all(engine):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _integrity_error()

    base = MagicMock()
    base.metadata.create_all.side_effect = _create_all
    cfg = DatabaseConfig(host="h", port=5432, name="d", user="u", password="p")

    with patch.object(db_mod, "create_engine", return_value=MagicMock()):
        db_mod.init_tables(cfg, base, max_attempts=3, backoff_seconds=0)

    assert calls["n"] == 2


def test_init_tables_raises_after_persistent_integrity_error():
    """Une vraie erreur de schéma (qui persiste) doit toujours faire échouer le boot."""
    base = MagicMock()
    base.metadata.create_all.side_effect = _integrity_error()
    cfg = DatabaseConfig(host="h", port=5432, name="d", user="u", password="p")

    with patch.object(db_mod, "create_engine", return_value=MagicMock()):
        with pytest.raises(IntegrityError):
            db_mod.init_tables(cfg, base, max_attempts=2, backoff_seconds=0)
