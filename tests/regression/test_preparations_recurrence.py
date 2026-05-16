"""Tests régression — Lot 6 récurrence (PR-4 du plan meeting-prep v2).

Couvre deux axes :

1. **Unit** sur le helper ``services/device-token-authority/app/recurrence.py``
   (importable sans Postgres / sans Flask app) : normalisation des règles +
   calcul ``compute_next_occurrence`` via ``python-dateutil``.

2. **HTTP smoke** sur les nouveaux endpoints exposés par ``mydevices-web``
   (``POST /api/preparations``, ``POST /api/preparations/<id>/amend`` —
   accepte désormais ``recurrence_rule`` / ``is_recurring`` / ``themes``).
   Sans cookie OIDC → 302/401, jamais 404 (preuve que le routeur connaît
   bien les nouveaux payloads ; le format est validé côté DTA).

Skip propre :
- ``dateutil`` manquant → skip module
- Service ``mydevices-web`` injoignable → skip section HTTP
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

import pytest

# ─── Path bootstrap pour importer le helper recurrence ──────────────────

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DTA_PATH = os.path.join(_REPO_ROOT, "services", "device-token-authority")
if _DTA_PATH not in sys.path:
    sys.path.insert(0, _DTA_PATH)

try:
    from app.recurrence import (  # type: ignore
        compute_next_occurrence,
        normalize_rule,
    )
    _HAS_RECURRENCE = True
    _IMPORT_ERR = None
except Exception as exc:  # pragma: no cover
    _HAS_RECURRENCE = False
    _IMPORT_ERR = str(exc)


pytestmark = pytest.mark.skipif(
    not _HAS_RECURRENCE,
    reason=f"app.recurrence indisponible : {_IMPORT_ERR}",
)


# ─── 1) normalize_rule — robustesse + nettoyage ──────────────────────────


def test_normalize_rule_minimal_weekly():
    out = normalize_rule({"freq": "weekly"})
    assert out is not None
    assert out["freq"] == "WEEKLY"
    assert out["interval"] == 1


def test_normalize_rule_unknown_freq_returns_none():
    assert normalize_rule({"freq": "yearly"}) is None
    assert normalize_rule({}) is None
    assert normalize_rule(None) is None
    assert normalize_rule("WEEKLY") is None  # type: ignore[arg-type]


def test_normalize_rule_byweekday_dedup_uppercase():
    out = normalize_rule({
        "freq": "WEEKLY",
        "byweekday": ["mo", "WE", "mo", "we", "XX", "fr"],
    })
    assert out["byweekday"] == ["MO", "WE", "FR"]


def test_normalize_rule_byhour_byminute_clamped():
    out = normalize_rule({"freq": "DAILY", "byhour": 25, "byminute": -1})
    # Valeurs hors range silencieusement ignorées
    assert "byhour" not in out and "byminute" not in out
    out = normalize_rule({"freq": "DAILY", "byhour": 14, "byminute": 30})
    assert out["byhour"] == 14 and out["byminute"] == 30


def test_normalize_rule_interval_clamped():
    assert normalize_rule({"freq": "DAILY", "interval": 0})["interval"] == 1
    assert normalize_rule({"freq": "DAILY", "interval": 999})["interval"] == 365


def test_normalize_rule_until_iso_date_only():
    out = normalize_rule({"freq": "WEEKLY", "until": "2026-12-31T23:59:59Z"})
    assert out["until"] == "2026-12-31"
    out = normalize_rule({"freq": "WEEKLY", "until": "garbage"})
    assert "until" not in out


# ─── 2) compute_next_occurrence — calcul ─────────────────────────────────


def test_next_occurrence_weekly_thursday_after_monday():
    """Hebdo, jeudi 14h, ref = lundi → suivante = jeudi suivant 14h."""
    rule = {"freq": "WEEKLY", "byweekday": ["TH"], "byhour": 14, "byminute": 0}
    reference = _dt.datetime(2026, 5, 18, 9, 0, tzinfo=_dt.timezone.utc)  # lundi
    now = reference
    nxt = compute_next_occurrence(rule, reference=reference, now=now)
    assert nxt is not None
    assert nxt.weekday() == 3  # jeudi
    assert nxt.hour == 14 and nxt.minute == 0
    assert nxt > now


def test_next_occurrence_daily_next_day():
    rule = {"freq": "DAILY", "byhour": 9, "byminute": 0}
    ref = _dt.datetime(2026, 5, 17, 10, 0, tzinfo=_dt.timezone.utc)
    nxt = compute_next_occurrence(rule, reference=ref, now=ref)
    assert nxt is not None and nxt > ref
    # daily 9h, ref = 10h, donc demain 9h
    assert nxt.date() == _dt.date(2026, 5, 18)
    assert nxt.hour == 9


def test_next_occurrence_until_in_past_returns_none():
    rule = {
        "freq": "WEEKLY", "byweekday": ["MO"],
        "until": "2026-05-10",  # avant la ref
    }
    ref = _dt.datetime(2026, 5, 17, tzinfo=_dt.timezone.utc)
    assert compute_next_occurrence(rule, reference=ref, now=ref) is None


def test_next_occurrence_invalid_rule_returns_none():
    assert compute_next_occurrence({"freq": "INVALID"}, reference=None) is None
    assert compute_next_occurrence(None, reference=None) is None


# ─── 3) HTTP smoke — endpoints recurrence/themes exposés ─────────────────

import requests  # noqa: E402

BASE_URL = os.getenv("MYDEVICES_WEB_URL", "http://localhost:8080")


@pytest.fixture(scope="module")
def _alive():
    try:
        r = requests.get(f"{BASE_URL}/healthz", timeout=2)
        r.raise_for_status()
    except Exception as exc:
        pytest.skip(f"mydevices-web not reachable at {BASE_URL}: {exc}")


def test_amend_endpoint_accepts_recurrence_payload_route_registered(_alive):
    """POST /api/preparations/<id>/amend doit exister (302/401 sans cookie,
    jamais 404). On vérifie que la route est enregistrée — la validation
    payload est testée en unit via normalize_rule."""
    pid = "00000000-0000-0000-0000-000000000000"
    r = requests.post(
        f"{BASE_URL}/api/preparations/{pid}/amend",
        json={"is_recurring": True,
              "recurrence_rule": {"freq": "WEEKLY", "byweekday": ["MO"]}},
        allow_redirects=False, timeout=5,
    )
    assert r.status_code != 404
    assert r.status_code in (302, 401)


def test_create_endpoint_route_registered(_alive):
    r = requests.post(
        f"{BASE_URL}/api/preparations",
        json={"title": "x", "is_recurring": True,
              "recurrence_rule": {"freq": "WEEKLY"}},
        allow_redirects=False, timeout=5,
    )
    assert r.status_code != 404
    assert r.status_code in (302, 401)
