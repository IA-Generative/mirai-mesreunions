"""Worker `video-ingest` — boucle de traitement Postgres-native (D13).

Trois threads :
  1. **main** : claim → run_and_record → boucle. Réveil sur `LISTEN/NOTIFY`
     côté `video_ingest_jobs`, fallback poll 5s.
  2. **heartbeat** : prolonge le lease du job en cours toutes les 30s.
  3. **watchdog** : toutes les 60s, réinitialise les jobs `running` dont
     le lease est dépassé (worker mort). Pattern aligné sur
     `services/dmz-to-internal-bridge/app/watchdog.py`.

Tous les threads sont des daemons → ils s'arrêtent quand le process
principal sort. Pas de signal handler custom : K8s envoie SIGTERM,
psycopg2 propage, on quitte proprement.
"""

from __future__ import annotations

import logging
import os
import select
import socket
import threading
import time

from . import db, jobs as jobs_mod, orchestrator
from .providers.youtube import YouTubeProvider

log = logging.getLogger(__name__)

NOTIFY_CHANNEL = "video_ingest_jobs"
LEASE_SECONDS = int(os.environ.get("VIDEO_INGEST_LEASE_SECONDS", "90"))
HEARTBEAT_INTERVAL = int(os.environ.get("VIDEO_INGEST_HEARTBEAT_INTERVAL", "30"))
WATCHDOG_INTERVAL = int(os.environ.get("VIDEO_INGEST_WATCHDOG_INTERVAL", "60"))
POLL_FALLBACK_TIMEOUT = int(os.environ.get("VIDEO_INGEST_POLL_TIMEOUT", "5"))

# Identité du worker pour `claimed_by` (utile pour debug et watchdog).
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

_PROVIDERS = [YouTubeProvider()]


def _start_heartbeat(stop_event: threading.Event, current_job_id_ref: dict) -> threading.Thread:
    def _loop():
        while not stop_event.wait(HEARTBEAT_INTERVAL):
            job_id = current_job_id_ref.get("id")
            if job_id is None:
                continue
            try:
                with db.connection() as conn:
                    if not jobs_mod.extend_lease(conn, job_id, lease_seconds=LEASE_SECONDS):
                        log.warning("heartbeat: job %s plus en running, abandon lease", job_id)
            except Exception:  # noqa: BLE001 — le heartbeat ne doit jamais tuer le worker
                log.exception("heartbeat: échec extend_lease pour job %s", job_id)
    t = threading.Thread(target=_loop, name="video-ingest-heartbeat", daemon=True)
    t.start()
    return t


def _start_watchdog(stop_event: threading.Event) -> threading.Thread:
    def _loop():
        while not stop_event.wait(WATCHDOG_INTERVAL):
            try:
                with db.connection() as conn:
                    n = jobs_mod.reset_orphans(conn)
                    if n:
                        log.warning("watchdog: %d job(s) orphelin(s) repassé(s) en pending", n)
            except Exception:
                log.exception("watchdog: échec reset_orphans")
    t = threading.Thread(target=_loop, name="video-ingest-watchdog", daemon=True)
    t.start()
    return t


def _wait_for_notification(timeout_s: int) -> None:
    """Bloque jusqu'à un NOTIFY ou expiration du timeout. Utilise une
    connexion dédiée en autocommit (requis pour LISTEN)."""
    with db.connection(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"LISTEN {NOTIFY_CHANNEL}")
        # `select` sur le file descriptor de la socket Postgres.
        if select.select([conn], [], [], timeout_s) != ([], [], []):
            conn.poll()
            # Vider la file pour éviter de boucler sur le même notify.
            while conn.notifies:
                conn.notifies.pop(0)


def run_forever(stop_event: threading.Event | None = None) -> None:
    """Point d'entrée principal du worker. Bloque jusqu'à `stop_event`."""
    # Fail-fast cohérent avec api.py create_app() : refuse de tourner
    # sans hook materialize quand on a déclaré qu'il est obligatoire.
    if os.environ.get("VIDEO_INGEST_MATERIALIZE_REQUIRED", "").lower() == "true":
        missing = [
            k for k in ("VIDEO_INGEST_MATERIALIZE_URL", "VIDEO_INGEST_INTERNAL_API_TOKEN")
            if not os.environ.get(k)
        ]
        if missing:
            raise RuntimeError(
                "VIDEO_INGEST_MATERIALIZE_REQUIRED=true mais env var(s) "
                f"manquante(s) : {', '.join(missing)}. Refuse de booter."
            )
    stop_event = stop_event or threading.Event()
    current_job_id_ref: dict = {"id": None}

    _start_heartbeat(stop_event, current_job_id_ref)
    _start_watchdog(stop_event)

    log.info("video-ingest worker démarré (%s)", WORKER_ID)

    while not stop_event.is_set():
        try:
            with db.connection() as conn:
                job = jobs_mod.claim_next(conn, claimed_by=WORKER_ID, lease_seconds=LEASE_SECONDS)
        except Exception:
            log.exception("claim_next a levé, attente avant retry")
            stop_event.wait(POLL_FALLBACK_TIMEOUT)
            continue

        if job is None:
            try:
                _wait_for_notification(POLL_FALLBACK_TIMEOUT)
            except Exception:
                log.exception("LISTEN/NOTIFY a échoué, fallback poll")
                stop_event.wait(POLL_FALLBACK_TIMEOUT)
            continue

        log.info("job %s claimed (url=%s)", job.id, job.url)
        current_job_id_ref["id"] = job.id
        try:
            with db.connection() as conn:
                orchestrator.run_and_record(conn, _PROVIDERS, job)
            log.info("job %s traité", job.id)
        except Exception:  # noqa: BLE001
            log.exception("job %s a explosé, marquage failed", job.id)
            try:
                with db.connection() as conn:
                    jobs_mod.fail(conn, job.id, error="unexpected_error")
            except Exception:
                log.exception("impossible de marquer le job %s failed", job.id)
        finally:
            current_job_id_ref["id"] = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(threadName)s] %(message)s")
    run_forever()
