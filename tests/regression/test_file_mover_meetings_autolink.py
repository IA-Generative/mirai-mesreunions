"""End-to-end tests pour la création de Meeting à l'upload audio (PR2d)
et l'auto-link audio↔preparation côté file-puller.

Exécution contre la stack docker compose locale (overlay shared-infra) :

    cd deploy/docker
    docker compose -f docker-compose.yml -f docker-compose.shared-infra.yml ps

Stratégie :
  - Setup/cleanup via psql ``docker exec`` sur ``owuicore-postgres-1``
    (port 5432 du host est partagé avec un brew postgres ; on évite le
    conflit en passant par le réseau interne docker).
  - Création de Preparation via HTTP token-issuer
    (``http://localhost:8091``).
  - Appel de ``auto_link_audio_to_preparation`` via ``docker exec`` dans
    le conteneur file-puller, avec un script Python qui charge le module
    déjà initialisé et exerce les helpers.

Couvre :
  1. Importabilité du module puller et présence des nouveaux symboles
     post-PR2d (Meeting, Preparation, auto_link_audio_to_preparation).
  2. Création de Meeting à l'upload (assert via SQL : pour chaque UAF
     fabriqué + Meeting fabriquée, la relation user_audio_file_id tient).
  3. Auto-link bout-en-bout : Preparation HTTP → UAF + Meeting fabriqués
     → script puller fait l'auto-link → on assert ``meeting.preparation_id``
     persisté.
  4. Anti-rebond : Preparation déjà liée → score anti_rebound=0 dans le
     candidate set.
  5. Pas de match si aucune candidate.

Skip module-wide si stack pas joignable.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
import uuid
from datetime import datetime, timezone

import pytest
import requests


BASE_URL = os.getenv("TOKEN_ISSUER_URL", "http://localhost:8091")
TOKEN = os.getenv("INTERNAL_API_TOKEN", "dev-only-internal-token-CHANGE-ME-IN-PROD")
H = {"Authorization": f"Bearer {TOKEN}"}

PG_CONTAINER = os.getenv("PG_CONTAINER", "owuicore-postgres-1")
PG_USER = "audio_int"
PG_DB = "audio_upload_int"
PULLER_CONTAINER = os.getenv("PULLER_CONTAINER", "docker-file-puller-1")


# ─── Helpers ────────────────────────────────────────────────────


def _psql(sql: str) -> str:
    res = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER,
         "psql", "-U", PG_USER, "-d", PG_DB, "-tA", "-c", sql],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"psql failed: {res.stderr}")
    return res.stdout.strip()


def _exec_in_puller(script: str) -> str:
    """Exécute un script Python dans le conteneur file-puller (où
    SessionLocal pointe déjà sur la bonne DB via la config compose).
    Retourne stdout."""
    res = subprocess.run(
        ["docker", "exec", "-i", PULLER_CONTAINER,
         "python", "-c", script],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"puller script failed: rc={res.returncode}\n"
            f"STDERR:\n{res.stderr}\nSTDOUT:\n{res.stdout}"
        )
    return res.stdout.strip()


def _cleanup(user_sub: str) -> None:
    """Hard-delete UAF + meetings + preparations pour ce user_sub."""
    _psql(f"DELETE FROM meetings WHERE user_sub = '{user_sub}';")
    _psql(f"DELETE FROM user_audio_files WHERE user_sub = '{user_sub}';")
    _psql(f"DELETE FROM preparations WHERE user_sub = '{user_sub}';")


@pytest.fixture(scope="module")
def alive():
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=2)
        r.raise_for_status()
    except Exception as exc:
        pytest.skip(f"token-issuer not reachable at {BASE_URL}: {exc}")
    # Vérifie aussi que les conteneurs auxiliaires existent.
    for c in (PG_CONTAINER, PULLER_CONTAINER):
        res = subprocess.run(
            ["docker", "inspect", c, "--format", "{{.State.Status}}"],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0 or "running" not in res.stdout:
            pytest.skip(f"container {c} not running: {res.stdout}{res.stderr}")


@pytest.fixture
def user_sub():
    return f"test-pr2d-{uuid.uuid4()}"


# ─── Scénario 1 : import sanity check post-PR2d ─────────────────


def test_puller_module_exposes_post_pr2d_symbols(alive):
    """Le module puller, importé dans le conteneur runtime, expose les
    nouveaux symboles canoniques (Meeting, Preparation,
    auto_link_audio_to_preparation) ET conserve l'alias rétrocompat
    auto_link_audio_to_brief."""
    out = _exec_in_puller(textwrap.dedent("""
        import app.puller as p
        symbols = []
        for name in ('Meeting', 'Preparation',
                     'auto_link_audio_to_preparation',
                     'auto_link_audio_to_brief',
                     '_fetch_preparation_glossary_terms'):
            assert hasattr(p, name), name
            symbols.append(name)
        # MeetingBrief alias doit avoir disparu.
        import libs.shared.app.models as m
        assert not hasattr(m, 'MeetingBrief'), 'MeetingBrief alias devrait être supprimé'
        print('OK', ','.join(symbols))
    """))
    assert out.startswith("OK"), out


# ─── Scénario 2 : auto-link sans candidate ──────────────────────


def test_auto_link_returns_none_when_no_candidate(alive, user_sub):
    try:
        out = _exec_in_puller(textwrap.dedent(f"""
            import uuid, json
            from datetime import datetime, timezone
            import app.puller as p
            r, scored = p.auto_link_audio_to_preparation(
                audio_id=uuid.uuid4(),
                user_sub='{user_sub}',
                audio_filename='anything.m4a',
                audio_upload_at=datetime.now(timezone.utc),
            )
            print(json.dumps({{'result': r, 'scored_count': len(scored)}}))
        """))
        data = json.loads(out.splitlines()[-1])
        assert data["result"] is None
        assert data["scored_count"] == 0
    finally:
        _cleanup(user_sub)


# ─── Scénario 3 : auto-link match déterministe via HTTP prep ────


def test_auto_link_matches_close_preparation(alive, user_sub):
    """Crée Preparation via HTTP, lance auto-link, vérifie match."""
    try:
        r = requests.post(
            f"{BASE_URL}/api/v1/preparations",
            json={
                "user_sub": user_sub,
                "subject": "COPIL DTNUM stratégie 2026",
                "title": "COPIL DTNUM 2026",
                "content": {"objective_reformulated": "Aligner DTNUM"},
            },
            headers=H,
        )
        assert r.status_code == 200, r.text
        prep_id = r.json()["preparation"]["id"]

        out = _exec_in_puller(textwrap.dedent(f"""
            import uuid, json
            from datetime import datetime, timezone
            import app.puller as p
            r, scored = p.auto_link_audio_to_preparation(
                audio_id=uuid.uuid4(),
                user_sub='{user_sub}',
                audio_filename='copil-dtnum-strategie-2026.m4a',
                audio_upload_at=datetime.now(timezone.utc),
            )
            print(json.dumps({{
                'result': r,
                'top_breakdown': scored[0]['breakdown'] if scored else None,
                'top_keys': sorted(list(scored[0].keys())) if scored else [],
            }}))
        """))
        data = json.loads(out.splitlines()[-1])
        assert data["result"] == prep_id, data
        # Le payload du candidat top expose à la fois preparation_id et
        # l'alias legacy brief_id.
        assert "preparation_id" in data["top_keys"]
        assert "brief_id" in data["top_keys"]
    finally:
        _cleanup(user_sub)


# ─── Scénario 4 : invariant UAF↔Meeting créés ensemble ──────────


def test_meeting_created_for_each_user_audio_file(alive, user_sub):
    """Simule la création atomique UAF + Meeting de ``_perform_pull`` (en
    SQL direct) et vérifie l'invariant : pour chaque UAF, il existe une
    Meeting telle que meeting.user_audio_file_id == uaf.id."""
    try:
        audio_id = str(uuid.uuid4())
        meeting_id = str(uuid.uuid4())
        _psql(
            f"INSERT INTO user_audio_files "
            f"(id, user_sub, original_session_code, original_filename, "
            f" stored_filename, file_size_bytes, transcription_status, "
            f" reprocess_version, reprocess_history) "
            f"VALUES ('{audio_id}', '{user_sub}', 'TESTPR2D', "
            f"'reunion.m4a', '{user_sub}/TESTPR2D/reunion.m4a', "
            f"123, 'pending', 0, '[]'::jsonb);"
        )
        _psql(
            f"INSERT INTO meetings "
            f"(id, user_sub, user_audio_file_id, preparation_id, title) "
            f"VALUES ('{meeting_id}', '{user_sub}', '{audio_id}', "
            f"NULL, 'reunion.m4a');"
        )

        # Vérification : la Meeting est trouvable via user_audio_file_id.
        found = _psql(
            f"SELECT id::text FROM meetings WHERE user_sub = '{user_sub}' "
            f"AND user_audio_file_id = '{audio_id}';"
        )
        assert found == meeting_id

        # preparation_id NULL à ce stade (auto-link pas encore tourné).
        prep = _psql(
            f"SELECT COALESCE(preparation_id::text, 'NULL') FROM meetings "
            f"WHERE id = '{meeting_id}';"
        )
        assert prep == "NULL"
    finally:
        _cleanup(user_sub)


# ─── Scénario 5 : auto-link bout-en-bout, persiste preparation_id ───


def test_auto_link_end_to_end_sets_meeting_preparation_id(alive, user_sub):
    try:
        # 1. Preparation via HTTP.
        r = requests.post(
            f"{BASE_URL}/api/v1/preparations",
            json={
                "user_sub": user_sub,
                "subject": "Réunion projet Federation kickoff",
                "title": "Federation kickoff",
            },
            headers=H,
        )
        assert r.status_code == 200, r.text
        prep_id = r.json()["preparation"]["id"]

        # 2. UAF + Meeting standalone (simule l'upload).
        audio_id = str(uuid.uuid4())
        meeting_id = str(uuid.uuid4())
        _psql(
            f"INSERT INTO user_audio_files "
            f"(id, user_sub, original_session_code, original_filename, "
            f" stored_filename, file_size_bytes, transcription_status, "
            f" reprocess_version, reprocess_history) "
            f"VALUES ('{audio_id}', '{user_sub}', 'TEST2D', "
            f"'reunion-federation-kickoff.m4a', "
            f"'{user_sub}/TEST2D/reunion-federation-kickoff.m4a', "
            f"42, 'pending', 0, '[]'::jsonb);"
        )
        _psql(
            f"INSERT INTO meetings "
            f"(id, user_sub, user_audio_file_id, title) "
            f"VALUES ('{meeting_id}', '{user_sub}', '{audio_id}', "
            f"'reunion-federation-kickoff.m4a');"
        )

        # 3. Auto-link + persistance dans le conteneur file-puller.
        out = _exec_in_puller(textwrap.dedent(f"""
            import json
            from datetime import datetime, timezone
            import app.puller as p
            from libs.shared.app.models import Meeting
            db = p.SessionLocal()
            try:
                r, scored = p.auto_link_audio_to_preparation(
                    audio_id='{audio_id}',
                    user_sub='{user_sub}',
                    audio_filename='reunion-federation-kickoff.m4a',
                    audio_upload_at=datetime.now(timezone.utc),
                    db=db,
                )
                m = db.query(Meeting).filter(Meeting.id == '{meeting_id}').first()
                if r:
                    m.preparation_id = r
                    db.commit()
                print(json.dumps({{'result': r, 'persisted': str(m.preparation_id) if m.preparation_id else None}}))
            finally:
                db.close()
        """))
        data = json.loads(out.splitlines()[-1])
        assert data["result"] == prep_id, data
        assert data["persisted"] == prep_id, data

        # 4. Re-lecture indépendante via SQL pour confirmer la persistance.
        persisted = _psql(
            f"SELECT preparation_id::text FROM meetings "
            f"WHERE id = '{meeting_id}';"
        )
        assert persisted == prep_id
    finally:
        _cleanup(user_sub)


# ─── Scénario 6 : anti-rebond ───────────────────────────────────


def test_auto_link_anti_rebound_excludes_already_linked_preparation(
    alive, user_sub,
):
    """Si une Preparation est déjà référencée par une Meeting du même
    user, le scoring lui pose anti_rebond=0."""
    try:
        # Prep #1 + Meeting déjà liée.
        r = requests.post(
            f"{BASE_URL}/api/v1/preparations",
            json={
                "user_sub": user_sub,
                "subject": "ancienne réunion",
                "title": "ancien",
            },
            headers=H,
        )
        prep_id = r.json()["preparation"]["id"]
        _psql(
            f"INSERT INTO meetings (id, user_sub, preparation_id, title) "
            f"VALUES ('{uuid.uuid4()}', '{user_sub}', '{prep_id}', "
            f"'déjà lié');"
        )

        out = _exec_in_puller(textwrap.dedent(f"""
            import uuid, json
            from datetime import datetime, timezone
            import app.puller as p
            r, scored = p.auto_link_audio_to_preparation(
                audio_id=uuid.uuid4(),
                user_sub='{user_sub}',
                audio_filename='ancienne-reunion.m4a',
                audio_upload_at=datetime.now(timezone.utc),
            )
            print(json.dumps({{
                'top_anti_rebound': scored[0]['breakdown']['anti_rebound'] if scored else None,
            }}))
        """))
        data = json.loads(out.splitlines()[-1])
        assert data["top_anti_rebound"] == 0.0, data
    finally:
        _cleanup(user_sub)
