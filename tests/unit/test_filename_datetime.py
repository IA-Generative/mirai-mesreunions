"""Tests pour extract_meeting_datetime — patterns supportés + rejets."""

from datetime import datetime, timezone, timedelta

import pytest

from libs.shared.app.filename_datetime import extract_meeting_datetime


PARIS = timezone(timedelta(hours=2))


@pytest.mark.parametrize("filename,expected", [
    # ISO 8601 flexible (séparateurs - / _ entre date, T/_/- entre date↔heure,
    # : / h / _ / - entre composants horaires).
    ("2026-05-21_14-30_reunion.m4a",        datetime(2026, 5, 21, 14, 30, tzinfo=PARIS)),
    ("2026-05-21T14:30:00.m4a",             datetime(2026, 5, 21, 14, 30, tzinfo=PARIS)),
    ("2026-05-21 14:30.m4a",                datetime(2026, 5, 21, 14, 30, tzinfo=PARIS)),
    ("2026/05/21 14:30.m4a",                datetime(2026, 5, 21, 14, 30, tzinfo=PARIS)),
    ("2026-05-21_14_29_49.m4a",             datetime(2026, 5, 21, 14, 29, 49, tzinfo=PARIS)),
    ("Cop'ia 2026-05-21_14_29_49.mp3",      datetime(2026, 5, 21, 14, 29, 49, tzinfo=PARIS)),
    # Compact YYYYMMDD_HHMMSS.
    ("20260521_143000.wav",                 datetime(2026, 5, 21, 14, 30, tzinfo=PARIS)),
    ("20260521-143000.m4a",                 datetime(2026, 5, 21, 14, 30, tzinfo=PARIS)),
    ("voice_20260521_140000.m4a",           datetime(2026, 5, 21, 14, 0, tzinfo=PARIS)),
    # Européen DD-MM-YYYY.
    ("21-05-2026_14h30.m4a",                datetime(2026, 5, 21, 14, 30, tzinfo=PARIS)),
    ("21/05/2026 14:30:00.m4a",             datetime(2026, 5, 21, 14, 30, tzinfo=PARIS)),
    ("Enregistrement_du_21-05-2026_à_14h30.m4a",
                                            datetime(2026, 5, 21, 14, 30, tzinfo=PARIS)),
    # Date seule (heure = 00:00).
    ("meeting_2026-05-21.mp3",              datetime(2026, 5, 21, 0, 0, tzinfo=PARIS)),
    ("21-05-2026.m4a",                      datetime(2026, 5, 21, 0, 0, tzinfo=PARIS)),
    # Bissextile valide.
    ("2024-02-29_14-30.m4a",                datetime(2024, 2, 29, 14, 30, tzinfo=PARIS)),
])
def test_extract_meeting_datetime_matches(filename, expected):
    result = extract_meeting_datetime(filename)
    assert result == expected, f"{filename}: got {result}, expected {expected}"


@pytest.mark.parametrize("filename", [
    "",
    "reunion sans date.m4a",
    "v1.2.3-final.mp3",
    "podcast_episode_2.mp3",
    # Année < 2000 → rejet
    "1999-12-31_14-30.m4a",
    # Futur lointain → rejet
    "2099-01-01_14-30.m4a",
    # Date invalide : 29 février non-bissextile
    "2026-02-29_14-30.m4a",
    # Date invalide : mois 13
    "2026-13-01.m4a",
    # Date invalide : jour 32
    "2026-05-32.m4a",
])
def test_extract_meeting_datetime_rejects(filename):
    assert extract_meeting_datetime(filename) is None, f"Expected None for {filename!r}"


@pytest.mark.parametrize("filename,expected_date", [
    # Heure invalide (25h) → fallback sur date seule à 00:00.
    ("2026-05-21_25-30.m4a", datetime(2026, 5, 21, 0, 0, tzinfo=PARIS)),
    # Minute invalide (99) → idem.
    ("2026-05-21_14-99.m4a", datetime(2026, 5, 21, 0, 0, tzinfo=PARIS)),
])
def test_invalid_time_falls_back_to_date_only(filename, expected_date):
    """Quand l'heure est invalide, on garde au moins la date utile."""
    assert extract_meeting_datetime(filename) == expected_date


def test_extract_returns_none_for_non_string():
    assert extract_meeting_datetime(None) is None
    assert extract_meeting_datetime(12345) is None


def test_custom_timezone():
    utc = timezone.utc
    result = extract_meeting_datetime("2026-05-21_14-30.m4a", tz=utc)
    assert result == datetime(2026, 5, 21, 14, 30, tzinfo=utc)


def test_iso_priority_over_eu_when_ambiguous():
    """20260521_143000 = compact → 21 mai 14:30, pas le 05-21 ambigu."""
    # Le pattern ISO matche en premier ; pour ce filename c'est compact qui
    # devrait s'appliquer. Le test est de s'assurer qu'on ne tombe pas dans
    # le pattern européen qui interpréterait DDMMYYYY.
    result = extract_meeting_datetime("20260521_143000.wav")
    assert result is not None
    assert result.year == 2026 and result.month == 5 and result.day == 21
