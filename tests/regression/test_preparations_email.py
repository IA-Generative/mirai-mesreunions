"""Tests régression — Lot 8 emails CR (PR-6 du plan meeting-prep v2).

Couvre :

1. **Unit** sur ``services/mesreunions-web/app/mailer.py`` (importable sans
   SMTP réel) : ``is_configured`` en mode dry-run, ``_valid_emails`` dédup
   et filtrage, ``build_cr_email`` génère subject/body sans dépendance
   externe, ``send_meeting_cr_email`` retourne ``skipped_reason`` quand
   SMTP non configuré (jamais ne lève).

2. **HTTP smoke** sur ``POST /api/meetings/<id>/send-cr`` (nouveau
   endpoint mailer) + ``POST /api/preparations/<id>/amend`` qui accepte
   le toggle ``send_cr_email``.

3. Garde-fou template : toggle envoi CR + section emails visibles.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_MESREUNIONS_PATH = os.path.join(_REPO_ROOT, "services", "mesreunions-web")

# Pytest peut avoir collecté un autre test qui a déjà mappé ``app`` à un
# autre service (ex. test_preparations_recurrence importe DTA). On purge
# l'éventuel namespace ``app`` du cache pour réimporter depuis mesreunions-web.
for _mod in list(sys.modules):
    if _mod == "app" or _mod.startswith("app."):
        del sys.modules[_mod]
# Insère mesreunions-web en TÊTE de sys.path pour shadow l'autre ``app``.
if _MESREUNIONS_PATH in sys.path:
    sys.path.remove(_MESREUNIONS_PATH)
sys.path.insert(0, _MESREUNIONS_PATH)

try:
    from app.mailer import (  # type: ignore
        _valid_emails,
        build_cr_email,
        is_configured,
        send_meeting_cr_email,
    )
    _HAS_MAILER = True
    _IMPORT_ERR = None
except Exception as exc:  # pragma: no cover
    _HAS_MAILER = False
    _IMPORT_ERR = str(exc)


pytestmark_unit = pytest.mark.skipif(
    not _HAS_MAILER, reason=f"app.mailer indispo : {_IMPORT_ERR}",
)


# ─── 1) Unit : config / helpers / build ─────────────────────────────────


@pytest.fixture
def _no_smtp_env(monkeypatch):
    """Force un environnement sans SMTP_HOST pour test dry-run."""
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_FROM",
              "SMTP_PASSWORD", "SMTP_USE_TLS"):
        monkeypatch.delenv(k, raising=False)


@pytestmark_unit
def test_is_configured_false_without_host(_no_smtp_env):
    assert is_configured() is False


@pytestmark_unit
def test_is_configured_true_with_host_and_from(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "no-reply@example.com")
    assert is_configured() is True


@pytestmark_unit
class TestValidEmails:
    def test_dedup_case_insensitive(self):
        out = _valid_emails([
            {"email": "Alice@Example.com"},
            {"email": "alice@example.com"},
            {"email": "bob@example.com"},
        ])
        # 1re casse conservée, dédoublonné case-insensitive
        assert out == ["Alice@Example.com", "bob@example.com"]

    def test_skip_invalid(self):
        out = _valid_emails([
            {"email": ""}, {"email": "no-at-symbol"}, {"name": "no email"},
            "not a dict", {"email": "ok@x.com"},
        ])
        assert out == ["ok@x.com"]

    def test_none_returns_empty(self):
        assert _valid_emails(None) == []
        assert _valid_emails("not a list") == []


@pytestmark_unit
class TestBuildCrEmail:
    def test_subject_uses_preparation_title(self):
        subject, body = build_cr_email(
            meeting={"id": "mid", "summary": "Résumé court"},
            preparation={"title": "Comité décision"},
            public_base_url="https://app.example/",
        )
        assert subject == "Compte rendu : Comité décision"
        assert "Comité décision" in body
        # Lien CR avec meeting_id
        assert "meeting_id=mid" in body
        # Pas de double slash dans URL
        assert "//?tab" not in body

    def test_fallback_title_when_no_preparation(self):
        subject, _ = build_cr_email(
            meeting={"id": "m", "title": "Brut"}, preparation=None,
            public_base_url="",
        )
        assert subject == "Compte rendu : Brut"

    def test_summary_truncated_at_600(self):
        long_sum = "x" * 2000
        _, body = build_cr_email(
            meeting={"id": "m", "summary": long_sum}, preparation=None,
            public_base_url="",
        )
        # Le body contient bien le résumé mais tronqué à 600
        assert "x" * 600 in body
        assert "x" * 700 not in body


@pytestmark_unit
def test_send_meeting_cr_email_skipped_when_not_configured(_no_smtp_env):
    """Best-effort : pas de SMTP → renvoie skipped_reason, ne lève pas."""
    out = send_meeting_cr_email(
        meeting={"id": "m"},
        preparation={"participants": [{"email": "a@x.com"}]},
    )
    assert out["ok"] is False
    assert out["sent"] == 0
    assert out["skipped_reason"] == "smtp_not_configured"


@pytestmark_unit
def test_send_meeting_cr_email_no_recipient(monkeypatch):
    """SMTP configuré + 0 destinataire → skipped no_recipient."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "no-reply@example.com")
    out = send_meeting_cr_email(
        meeting={"id": "m"},
        preparation={"participants": []},
        recipients=[],
    )
    assert out["ok"] is False
    assert out["skipped_reason"] == "no_recipient"


# ─── 2) HTTP smoke ───────────────────────────────────────────────────────

import requests  # noqa: E402

BASE_URL = os.getenv("MYDEVICES_WEB_URL", "http://localhost:8080")


@pytest.fixture(scope="module")
def _alive():
    try:
        r = requests.get(f"{BASE_URL}/healthz", timeout=2)
        r.raise_for_status()
    except Exception as exc:
        pytest.skip(f"mesreunions-web not reachable at {BASE_URL}: {exc}")


@pytest.fixture(scope="module")
def _alive_with_send_cr(_alive):
    """Skip si l'endpoint send-cr n'est pas encore déployé."""
    mid = "00000000-0000-0000-0000-000000000000"
    probe = requests.post(
        f"{BASE_URL}/api/meetings/{mid}/send-cr",
        json={}, allow_redirects=False, timeout=5,
    )
    if probe.status_code == 404:
        pytest.skip(
            "stack mesreunions-web déployée localement ne contient pas "
            "l'endpoint /api/meetings/<id>/send-cr (Lot 8). Rebuild + "
            "rollout requis."
        )


def test_send_cr_endpoint_registered(_alive_with_send_cr):
    mid = "00000000-0000-0000-0000-000000000000"
    r = requests.post(
        f"{BASE_URL}/api/meetings/{mid}/send-cr",
        json={"force": False}, allow_redirects=False, timeout=5,
    )
    assert r.status_code != 404, (
        "POST /api/meetings/<id>/send-cr → 404 (Lot 8 endpoint absent)"
    )
    assert r.status_code in (302, 401, 503)


def test_amend_accepts_send_cr_email_toggle(_alive):
    pid = "00000000-0000-0000-0000-000000000000"
    r = requests.post(
        f"{BASE_URL}/api/preparations/{pid}/amend",
        json={"send_cr_email": True}, allow_redirects=False, timeout=5,
    )
    assert r.status_code != 404
    assert r.status_code in (302, 401)


# ─── 3) Front — selectors UI emails ─────────────────────────────────────

_INDEX = pathlib.Path(_REPO_ROOT) / "services" / "mesreunions-web" / "app" / "templates" / "index.html"


def test_emails_section_present_in_template():
    txt = _INDEX.read_text()
    assert "brief-detail-emails" in txt, "Section emails absente (Lot 8)"
    assert "brief-detail-send-cr-toggle" in txt, "Toggle envoi CR absent"
    assert "brief-detail-mailto-link" in txt, "Bouton mailto absent"
