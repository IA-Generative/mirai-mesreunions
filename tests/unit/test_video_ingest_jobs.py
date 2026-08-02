"""Tests des helpers `jobs.py` — vérifie les requêtes SQL clés via un
mock de curseur (pas de vrai Postgres en unit test).
"""

from unittest.mock import MagicMock

from services.video_ingest.app import jobs


def _conn_with_cursor():
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_enqueue_inserts_and_notifies():
    conn, cur = _conn_with_cursor()
    cur.fetchone.return_value = (42,)

    job_id = jobs.enqueue(
        conn, url="https://youtu.be/x", user_sub="u-1",
        context="meeting", context_id="m-9",
        language_pref="fr", force_audio=False,
    )

    assert job_id == 42
    assert cur.execute.call_count == 2  # INSERT + NOTIFY
    insert_sql = cur.execute.call_args_list[0].args[0]
    notify_sql = cur.execute.call_args_list[1].args[0]
    assert "INSERT INTO video_ingest_jobs" in insert_sql
    assert notify_sql.startswith("NOTIFY video_ingest_jobs")
    assert cur.execute.call_args_list[1].args[1] == ("42",)


def test_claim_next_returns_none_when_no_job():
    conn, cur = _conn_with_cursor()
    cur.fetchone.return_value = None

    assert jobs.claim_next(conn, claimed_by="pod-1") is None
    sql = cur.execute.call_args.args[0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "status = 'pending'" in sql
    assert "lease_until < NOW()" in sql


def test_claim_next_returns_job_when_available():
    conn, cur = _conn_with_cursor()
    cur.fetchone.return_value = (
        7, "https://youtu.be/x", "u-1", "meeting", "m-9", "fr", False, 1,
    )
    job = jobs.claim_next(conn, claimed_by="pod-1", lease_seconds=120)
    assert job is not None
    assert job.id == 7
    assert job.url == "https://youtu.be/x"
    assert job.user_sub == "u-1"
    assert job.context == "meeting"
    assert job.language_pref == "fr"
    assert job.force_audio is False
    assert job.attempts == 1
    # Vérifie que le lease_seconds a bien été interpolé
    args = cur.execute.call_args.args
    assert args[1] == ("pod-1", "120")


def test_extend_lease_returns_true_when_running():
    conn, cur = _conn_with_cursor()
    cur.rowcount = 1
    assert jobs.extend_lease(conn, 42, lease_seconds=60) is True
    sql = cur.execute.call_args.args[0]
    assert "status = 'running'" in sql


def test_extend_lease_returns_false_when_already_terminal():
    conn, cur = _conn_with_cursor()
    cur.rowcount = 0
    assert jobs.extend_lease(conn, 42) is False


def test_complete_sets_done_and_records_reused():
    conn, cur = _conn_with_cursor()
    jobs.complete(conn, 42, video_source_id=99, reused=True)
    args = cur.execute.call_args.args
    assert "status          = 'done'" in args[0]
    assert args[1] == (99, True, 42)


def test_fail_truncates_error_message():
    conn, cur = _conn_with_cursor()
    huge = "x" * 5000
    jobs.fail(conn, 42, error=huge)
    args = cur.execute.call_args.args
    assert len(args[1][0]) == 2000


def test_fail_handles_none_error_gracefully():
    conn, cur = _conn_with_cursor()
    jobs.fail(conn, 42, error="")
    # Pas de crash, l'INSERT passe avec NULL
    args = cur.execute.call_args.args
    assert args[1][0] is None


def test_retry_reprogramme_sans_etat_terminal():
    """Incident anti-bot 2026-08-02 : le job repart en `pending` avec un
    réarmement futur, et surtout PAS en `failed`."""
    conn, cur = _conn_with_cursor()
    jobs.retry(conn, 31, error="transient: anti-bot", delay_seconds=20)
    sql, params = cur.execute.call_args.args
    assert "status          = 'pending'" in sql
    assert "next_attempt_at = NOW() + (%s || ' seconds')::INTERVAL" in sql
    assert "'failed'" not in sql
    assert "completed_at" not in sql        # le job n'est pas fini
    assert params == ("20", "transient: anti-bot", 31)


def test_retry_libere_le_lease_et_le_claim():
    """Sans ça, le job resterait attribué au pod qui vient d'échouer."""
    conn, cur = _conn_with_cursor()
    jobs.retry(conn, 31, error="x", delay_seconds=60)
    sql = cur.execute.call_args.args[0]
    assert "claimed_by      = NULL" in sql
    assert "lease_until     = NULL" in sql


def test_retry_tronque_le_message():
    conn, cur = _conn_with_cursor()
    jobs.retry(conn, 31, error="x" * 5000, delay_seconds=20)
    assert len(cur.execute.call_args.args[1][1]) == 2000


def test_claim_next_ignore_les_jobs_en_backoff():
    """Un job réarmé dans le futur ne doit pas être repris tout de suite —
    sinon le backoff dégénère en boucle serrée contre YouTube."""
    conn, cur = _conn_with_cursor()
    cur.fetchone.return_value = None
    jobs.claim_next(conn, claimed_by="pod-1")
    sql = cur.execute.call_args.args[0]
    assert "next_attempt_at IS NULL OR next_attempt_at <= NOW()" in sql


def test_reset_orphans_returns_rowcount():
    conn, cur = _conn_with_cursor()
    cur.rowcount = 3
    assert jobs.reset_orphans(conn) == 3
    sql = cur.execute.call_args.args[0]
    assert "status = 'running'" in sql
    assert "lease_until < NOW()" in sql
    assert "status     = 'pending'" in sql
