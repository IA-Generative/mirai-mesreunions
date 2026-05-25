"""Tests des quotas (Q4) — fenêtre 24h."""

from unittest.mock import MagicMock

import pytest

from services.video_ingest.app import quotas


def _conn_with_count(n):
    cur = MagicMock()
    cur.fetchone.return_value = (n,)
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def test_under_limit_passes(monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_QUOTA_IMPORTS_PER_DAY", "10")
    quotas.check_import_quota(_conn_with_count(3), user_sub="u")


def test_at_limit_raises(monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_QUOTA_IMPORTS_PER_DAY", "10")
    with pytest.raises(quotas.QuotaExceeded) as e:
        quotas.check_import_quota(_conn_with_count(10), user_sub="u")
    assert e.value.limit == 10
    assert e.value.current == 10


def test_zero_means_disabled(monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_QUOTA_IMPORTS_PER_DAY", "0")
    # Même avec 9999 jobs, no-op
    quotas.check_import_quota(_conn_with_count(9999), user_sub="u")


def test_default_when_unset(monkeypatch):
    monkeypatch.delenv("VIDEO_INGEST_QUOTA_IMPORTS_PER_DAY", raising=False)
    # défaut 50 — sous la limite
    quotas.check_import_quota(_conn_with_count(49), user_sub="u")
    # à la limite → raise
    with pytest.raises(quotas.QuotaExceeded):
        quotas.check_import_quota(_conn_with_count(50), user_sub="u")
