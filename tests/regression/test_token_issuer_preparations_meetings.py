"""End-to-end tests pour les endpoints `/api/v1/{preparations,meetings}/*`
du token-issuer (PR2c).

Exécution contre la stack docker compose locale (overlay shared-infra) :

    cd deploy/docker
    docker compose -f docker-compose.yml -f docker-compose.shared-infra.yml ps

Token-issuer est exposé sur ``http://localhost:8091``. Auth par
INTERNAL_API_TOKEN bearer (sync avec compose env).

Couvre les 6 scénarios demandés par le prompt PR2c :
  1. Auth 401 sans bearer
  2. Happy path preparation : CREATE → GET → UPDATE → DELETE (soft)
  3. Meeting standalone (sans audio, sans prep) → assert preparation_id NULL
  4. Meeting avec preparation_id préalable → assert lien OK
  5. Auto-link audio↔préparation via /api/v1/files/by-id/link-preparation
     (audio créé en base directement, link déclenche création meeting +
     set preparation_id)
  6. Isolation user_sub : un autre user ne voit pas la préparation
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import requests

# ─── Configuration ──────────────────────────────────────────

BASE_URL = os.getenv("TOKEN_ISSUER_URL", "http://localhost:8091")
TOKEN = os.getenv("INTERNAL_API_TOKEN", "dev-only-internal-token-CHANGE-ME-IN-PROD")
H = {"Authorization": f"Bearer {TOKEN}"}

# Postgres direct (pour fabriquer un user_audio_files row dans le test 5).
PG_CONTAINER = os.getenv("PG_CONTAINER", "owuicore-postgres-1")


def _pg_exec(sql: str) -> str:
    """Exécute du SQL via docker exec et renvoie stdout."""
    import subprocess
    res = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER,
         "psql", "-U", "audio_int", "-d", "audio_upload_int",
         "-tA", "-c", sql],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"psql failed: {res.stderr}")
    return res.stdout.strip()


@pytest.fixture(scope="module")
def alive():
    """Skip toute la suite si la stack n'est pas joignable."""
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=2)
        r.raise_for_status()
    except Exception as exc:
        pytest.skip(f"token-issuer not reachable at {BASE_URL}: {exc}")


@pytest.fixture
def user_sub():
    return f"test-pr2c-{uuid.uuid4()}"


@pytest.fixture
def other_user_sub():
    return f"test-pr2c-other-{uuid.uuid4()}"


# ─── Scénario 1 : Auth ──────────────────────────────────────


def test_auth_401_sans_bearer(alive):
    """Sans Authorization → 401."""
    r = requests.get(f"{BASE_URL}/api/v1/preparations", params={"user_sub": "x"})
    assert r.status_code == 401, r.text

    r = requests.get(f"{BASE_URL}/api/v1/meetings", params={"user_sub": "x"})
    assert r.status_code == 401, r.text


# ─── Scénario 2 : Preparation CRUD ──────────────────────────


def test_preparation_full_lifecycle(alive, user_sub):
    """CREATE → GET → UPDATE (rename + amend) → DELETE soft → RESTORE → trash → PURGE."""
    # CREATE
    payload = {
        "user_sub": user_sub,
        "subject": "Préparer entretien annuel",
        "role": "Manager",
        "expectation": "Aligner sur objectifs Q3",
        "duration_minutes": 45,
        "focus": ["formation", "mobilité"],
        "content": {"objective_reformulated": "Cadrer la trajectoire."},
        "documents": [{"name": "fiche.md", "size": 1234}],
        "title": "Entretien annuel J.D.",
    }
    r = requests.post(f"{BASE_URL}/api/v1/preparations", json=payload, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    prep = body["preparation"]
    pid = prep["id"]
    assert prep["title"] == "Entretien annuel J.D."
    assert prep["content"] == {"objective_reformulated": "Cadrer la trajectoire."}
    assert prep["trashed_at"] is None

    # GET listing
    r = requests.get(
        f"{BASE_URL}/api/v1/preparations",
        params={"user_sub": user_sub},
        headers=H,
    )
    assert r.status_code == 200
    listing = r.json()["preparations"]
    assert any(p["id"] == pid for p in listing)

    # GET détail
    r = requests.get(
        f"{BASE_URL}/api/v1/preparations/{pid}",
        params={"user_sub": user_sub, "track_view": "false"},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["preparation"]["id"] == pid

    # UPDATE rename
    r = requests.post(
        f"{BASE_URL}/api/v1/preparations/{pid}/rename",
        json={"user_sub": user_sub, "title": "Entretien J.D. v2"},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Entretien J.D. v2"

    # UPDATE amend
    r = requests.post(
        f"{BASE_URL}/api/v1/preparations/{pid}/amend",
        json={"user_sub": user_sub, "content": {"agenda": ["A", "B"]}},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["preparation"]["content"] == {"agenda": ["A", "B"]}

    # DELETE soft
    r = requests.delete(
        f"{BASE_URL}/api/v1/preparations/{pid}",
        json={"user_sub": user_sub},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["trashed"] is True

    # Plus visible dans le listing actif
    r = requests.get(
        f"{BASE_URL}/api/v1/preparations",
        params={"user_sub": user_sub},
        headers=H,
    )
    assert all(p["id"] != pid for p in r.json()["preparations"])

    # Visible dans le listing corbeille
    r = requests.get(
        f"{BASE_URL}/api/v1/preparations",
        params={"user_sub": user_sub, "trashed": "true"},
        headers=H,
    )
    assert any(p["id"] == pid for p in r.json()["preparations"])

    # RESTORE
    r = requests.post(
        f"{BASE_URL}/api/v1/preparations/{pid}/restore",
        json={"user_sub": user_sub},
        headers=H,
    )
    assert r.status_code == 200

    # Re-trash puis purge immédiat
    requests.delete(
        f"{BASE_URL}/api/v1/preparations/{pid}",
        json={"user_sub": user_sub},
        headers=H,
    )
    # Bypasse le seuil 30j en utilisant older_than_days=0 — mais l'endpoint
    # exige min=1, donc on hard-delete via /permanently.
    r = requests.delete(
        f"{BASE_URL}/api/v1/preparations/{pid}/permanently",
        json={"user_sub": user_sub},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["deleted"] is True


# ─── Scénario 3 : Meeting standalone ────────────────────────


def test_meeting_standalone_pas_de_prep_ni_audio(alive, user_sub):
    """Création d'un CR manuel : pas d'audio, pas de prep."""
    r = requests.post(
        f"{BASE_URL}/api/v1/meetings",
        json={
            "user_sub": user_sub,
            "title": "Standup ad-hoc",
            "summary": "Décision : reporter le release.",
            "content": {"decisions": ["report v2.3"]},
        },
        headers=H,
    )
    assert r.status_code == 200, r.text
    m = r.json()["meeting"]
    assert m["user_audio_file_id"] is None
    assert m["preparation_id"] is None
    assert m["title"] == "Standup ad-hoc"
    assert m["content"] == {"decisions": ["report v2.3"]}

    # Cleanup
    requests.delete(
        f"{BASE_URL}/api/v1/meetings/{m['id']}",
        json={"user_sub": user_sub},
        headers=H,
    )
    requests.delete(
        f"{BASE_URL}/api/v1/meetings/{m['id']}/permanently",
        json={"user_sub": user_sub},
        headers=H,
    )


# ─── Scénario 4 : Meeting + preparation_id ──────────────────


def test_meeting_with_preparation_link(alive, user_sub):
    """Crée prep, crée meeting avec preparation_id → assert le lien."""
    # Préparation amont.
    r = requests.post(
        f"{BASE_URL}/api/v1/preparations",
        json={"user_sub": user_sub, "subject": "Comité de pilotage", "title": "Copil S22"},
        headers=H,
    )
    pid = r.json()["preparation"]["id"]

    # Meeting lié.
    r = requests.post(
        f"{BASE_URL}/api/v1/meetings",
        json={
            "user_sub": user_sub,
            "title": "Copil S22 CR",
            "preparation_id": pid,
            "content": {"summary": "OK pour passer en prod."},
        },
        headers=H,
    )
    assert r.status_code == 200, r.text
    mid = r.json()["meeting"]["id"]
    assert r.json()["meeting"]["preparation_id"] == pid

    # link-preparation : détache.
    r = requests.post(
        f"{BASE_URL}/api/v1/meetings/{mid}/link-preparation",
        json={"user_sub": user_sub, "preparation_id": None},
        headers=H,
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_preparation_id"] is None
    assert r.json()["previous_preparation_id"] == pid

    # Re-attache via link-preparation.
    r = requests.post(
        f"{BASE_URL}/api/v1/meetings/{mid}/link-preparation",
        json={"user_sub": user_sub, "preparation_id": pid},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["new_preparation_id"] == pid

    # Cleanup
    for url in (
        f"{BASE_URL}/api/v1/meetings/{mid}",
        f"{BASE_URL}/api/v1/preparations/{pid}",
    ):
        requests.delete(url, json={"user_sub": user_sub}, headers=H)
    for url in (
        f"{BASE_URL}/api/v1/meetings/{mid}/permanently",
        f"{BASE_URL}/api/v1/preparations/{pid}/permanently",
    ):
        requests.delete(url, json={"user_sub": user_sub}, headers=H)


# ─── Scénario 5 : Auto-link audio↔préparation ───────────────


def test_link_audio_to_preparation_creates_meeting(alive, user_sub):
    """Crée prep + UserAudioFile direct en DB ; appel link-preparation crée
    un meeting et set preparation_id ; UAF.meeting_id est mis à jour."""
    # Préparation amont.
    r = requests.post(
        f"{BASE_URL}/api/v1/preparations",
        json={"user_sub": user_sub, "subject": "Réunion projet X", "title": "Projet X kickoff"},
        headers=H,
    )
    pid = r.json()["preparation"]["id"]

    # Fabrique un UserAudioFile minimal en DB (token-issuer n'expose pas
    # de POST UAF dédié — c'est file-mover qui le fait normalement).
    audio_id = str(uuid.uuid4())
    _pg_exec(
        f"""
        INSERT INTO user_audio_files (
            id, user_sub, original_session_code, original_filename,
            stored_filename, file_size_bytes, transcription_status,
            reprocess_version, reprocess_history, created_at, pulled_at
        ) VALUES (
            '{audio_id}', '{user_sub}', 'TST123', 'projet-x.m4a',
            'TST123_projet-x.m4a', 12345, 'pending', 0, '[]', now(), now()
        );
        """
    )

    try:
        # Avant link : UAF.meeting_id IS NULL.
        meeting_id_db = _pg_exec(
            f"SELECT meeting_id FROM user_audio_files WHERE id = '{audio_id}';"
        )
        assert meeting_id_db == "" or meeting_id_db is None

        # Déclenche link-preparation.
        r = requests.post(
            f"{BASE_URL}/api/v1/files/by-id/link-preparation",
            json={"user_sub": user_sub, "file_id": audio_id, "preparation_id": pid},
            headers=H,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["new_preparation_id"] == pid
        assert body["meeting_id"], "meeting_id should be returned"
        mid = body["meeting_id"]

        # UAF.meeting_id pointe désormais vers le meeting créé.
        meeting_id_db = _pg_exec(
            f"SELECT meeting_id FROM user_audio_files WHERE id = '{audio_id}';"
        )
        assert meeting_id_db == mid

        # Meeting.preparation_id pointe vers la prep ; meeting.user_audio_file_id
        # pointe vers l'audio.
        prep_id_db = _pg_exec(f"SELECT preparation_id FROM meetings WHERE id = '{mid}';")
        audio_id_db = _pg_exec(f"SELECT user_audio_file_id FROM meetings WHERE id = '{mid}';")
        assert prep_id_db == pid
        assert audio_id_db == audio_id

        # audio-files listing pour cette prep contient l'audio.
        r = requests.get(
            f"{BASE_URL}/api/v1/preparations/{pid}/audio-files",
            params={"user_sub": user_sub},
            headers=H,
        )
        assert r.status_code == 200, r.text
        rows = r.json()["audio_files"]
        assert any(row["id"] == audio_id for row in rows)

        # Détache.
        r = requests.post(
            f"{BASE_URL}/api/v1/files/by-id/link-preparation",
            json={"user_sub": user_sub, "file_id": audio_id, "preparation_id": None},
            headers=H,
        )
        assert r.status_code == 200
        assert r.json()["new_preparation_id"] is None

    finally:
        # Cleanup : delete UAF puis prep+meeting.
        _pg_exec(f"DELETE FROM user_audio_files WHERE id = '{audio_id}';")
        _pg_exec(f"DELETE FROM meetings WHERE user_sub = '{user_sub}';")
        _pg_exec(f"DELETE FROM preparations WHERE user_sub = '{user_sub}';")


# ─── Scénario 6 : Isolation user_sub ────────────────────────


def test_isolation_user_sub(alive, user_sub, other_user_sub):
    """User A crée une préparation ; user B ne doit pas la voir / l'éditer."""
    r = requests.post(
        f"{BASE_URL}/api/v1/preparations",
        json={"user_sub": user_sub, "subject": "Confidentiel A", "title": "Strict A"},
        headers=H,
    )
    pid = r.json()["preparation"]["id"]

    try:
        # User B : GET → 404
        r = requests.get(
            f"{BASE_URL}/api/v1/preparations/{pid}",
            params={"user_sub": other_user_sub},
            headers=H,
        )
        assert r.status_code == 404

        # User B : listing → ne contient pas pid
        r = requests.get(
            f"{BASE_URL}/api/v1/preparations",
            params={"user_sub": other_user_sub},
            headers=H,
        )
        assert all(p["id"] != pid for p in r.json()["preparations"])

        # User B : rename → 404
        r = requests.post(
            f"{BASE_URL}/api/v1/preparations/{pid}/rename",
            json={"user_sub": other_user_sub, "title": "Pwned"},
            headers=H,
        )
        assert r.status_code == 404

        # User B : delete → 404
        r = requests.delete(
            f"{BASE_URL}/api/v1/preparations/{pid}",
            json={"user_sub": other_user_sub},
            headers=H,
        )
        assert r.status_code == 404

    finally:
        # Cleanup user A.
        requests.delete(
            f"{BASE_URL}/api/v1/preparations/{pid}",
            json={"user_sub": user_sub},
            headers=H,
        )
        requests.delete(
            f"{BASE_URL}/api/v1/preparations/{pid}/permanently",
            json={"user_sub": user_sub},
            headers=H,
        )
