"""Tests du module audit — l'écriture ne doit jamais lever."""

import logging
from unittest.mock import MagicMock

from services.video_ingest.app import audit


def _conn_ok():
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_log_event_nominal_writes_insert():
    conn, cur = _conn_ok()
    audit.log_event(
        conn, action="import", user_sub="u-1", url="https://youtu.be/x",
        reused=False, job_id=42, context="meeting", context_id="m-9",
        details={"force_audio": False},
    )
    cur.execute.assert_called_once()
    sql = cur.execute.call_args.args[0]
    assert "INSERT INTO video_ingest_audit" in sql


def test_log_event_swallows_exceptions(caplog):
    """Si l'INSERT casse (BDD HS), l'audit ne doit pas tuer le flux."""
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("BDD HS")
    with caplog.at_level(logging.ERROR):
        audit.log_event(conn, action="import", user_sub="u-1")
    # Pas d'exception remontée
    assert any("audit log failed" in r.message for r in caplog.records)
