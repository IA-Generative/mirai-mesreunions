"""Pipeline watchdog — détecte et reprend automatiquement les jobs
orphelins (pod tué pendant un poll Kevent, transitoire réseau, etc.).

Mécanisme :
- Tous les WATCHDOG_INTERVAL_S (30s), scan ``user_audio_files`` pour repérer
  les rows :
    * dont ``transcription_status`` est non-terminal (kevent_queued,
      kevent_processing, kevent_transcribing, transcoding, pending, …)
    * ET ``last_activity_at < NOW() - STALE_THRESHOLD_S`` (5 min par défaut)
- Pour chaque row repérée, claim atomique via UPDATE :
    UPDATE … SET pipeline_claim_at = NOW(), pipeline_claim_pod = $POD
    WHERE id = $id AND (pipeline_claim_at IS NULL OR pipeline_claim_at < NOW() - 90s)
  Le premier pod gagne ; les autres récupèrent rowcount=0 et passent.
- Pour le claim : appel de ``_reset_and_resubmit_kevent_pipeline`` qui réutilise
  le même moteur que ``/api/v1/audio/<id>/full-reprocess``. Le pipeline
  retourne à 0 (nouveau job Kevent submitted + LLM chain rejouée).
- Le claim expire automatiquement à NOW()+90s, donc si le pod meurt avant
  d'avoir lancé le re-pipeline, un autre pod reprendra.

Lancé en thread daemon depuis le boot du puller, géré par
``start_watchdog()`` qui no-op si déjà démarré ou si désactivé via env
``PIPELINE_WATCHDOG_DISABLED=1`` (utile en tests).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import text as sql_text

logger = logging.getLogger("pipeline_watchdog")

WATCHDOG_INTERVAL_S = int(os.environ.get("PIPELINE_WATCHDOG_INTERVAL_S", "30"))
STALE_THRESHOLD_S = int(os.environ.get("PIPELINE_STALE_THRESHOLD_S", "300"))  # 5 min
CLAIM_LEASE_S = int(os.environ.get("PIPELINE_CLAIM_LEASE_S", "90"))
SCAN_BATCH_LIMIT = int(os.environ.get("PIPELINE_SCAN_BATCH_LIMIT", "20"))
MAX_AGE_HOURS = int(os.environ.get("PIPELINE_MAX_AGE_HOURS", "168"))  # 7 jours
# Cap dur sur le nombre de retentatives auto. Au-delà, marquer
# kevent_failed et stop. Évite la boucle infinie quand un job ne
# revient JAMAIS du pipeline (Kevent rejette silencieusement, audio
# corrompu, race interne...). L'utilisateur peut toujours forcer un
# retry manuel via le bouton "Relancer les bloqués" (include_failed).
MAX_AUTO_RETRIES = int(os.environ.get("PIPELINE_MAX_AUTO_RETRIES", "5"))

# Statuts non-terminaux (= job en cours). Si la row est dans un de ces
# états ET inactive depuis STALE_THRESHOLD_S, c'est un orphelin candidat.
NON_TERMINAL_STATUSES = (
    "pending",
    "transferring",
    "transcoding",
    "kevent_queued",
    "kevent_transcribing",
    "kevent_processing",
    # Import MCR : la row est créée en pending ; si le worker mcr_importer
    # n'aboutit pas (timeout, MCR transitoire), elle reste bloquée ici.
    "mcr_import_pending",
)

# Statuts terminaux qu'on peut RE-tenter si l'utilisateur le demande
# explicitement (bouton "Relancer les bloqués"). Pas inclus dans le tick
# automatique pour éviter une boucle de retry infinie sur un audio
# vraiment cassé (S3 purgé, format non supporté, etc.).
RETRYABLE_TERMINAL_STATUSES = (
    "kevent_failed",
    "failed",
    "mcr_import_failed",
)

_POD_HOSTNAME = os.environ.get("HOSTNAME") or socket.gethostname()
_watchdog_started = False
_watchdog_lock = threading.Lock()


def _scan_stuck(session_factory, *, user_sub: Optional[str] = None,
                limit: int = SCAN_BATCH_LIMIT,
                include_failed: bool = False) -> list[Tuple[str, str]]:
    """Retourne ``[(audio_id, user_sub), ...]`` des jobs candidats à reprise.

    Filtre :
      - status non-terminal (toujours), + ``kevent_failed``/``failed`` si
        ``include_failed=True`` (mode manuel "relancer les bloqués")
      - last_activity_at < NOW() - STALE_THRESHOLD_S
      - created_at > NOW() - MAX_AGE_HOURS (évite les très vieux fichiers
        dont S3 a probablement été purgé)
      - (optionnel) user_sub précis si fourni
      - claim libre OU expiré (autre pod a peut-être abandonné)
    """
    db = session_factory()
    try:
        stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_THRESHOLD_S)
        age_cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
        claim_cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_LEASE_S)

        statuses = list(NON_TERMINAL_STATUSES)
        if include_failed:
            statuses = statuses + list(RETRYABLE_TERMINAL_STATUSES)

        # Cap auto : on filtre les rows qui ont déjà été relancées plus
        # de MAX_AUTO_RETRIES fois. Quand include_failed=True (mode user
        # explicite), on ignore ce cap : l'utilisateur peut toujours
        # forcer un dernier essai.
        retry_cap_clause = ""
        if not include_failed:
            retry_cap_clause = "AND COALESCE(reprocess_version, 0) < :max_retries"

        q = sql_text(("""
            SELECT id::text, user_sub
              FROM user_audio_files
             WHERE transcription_status = ANY(:statuses)
               AND (last_activity_at IS NULL OR last_activity_at < :stale_cutoff)
               AND created_at > :age_cutoff
               AND (pipeline_claim_at IS NULL OR pipeline_claim_at < :claim_cutoff)
               {retry_cap}
               {user_filter}
             ORDER BY last_activity_at ASC NULLS FIRST
             LIMIT :limit
        """).replace("{retry_cap}", retry_cap_clause)
            .replace("{user_filter}", "AND user_sub = :user_sub" if user_sub else ""))
        params = {
            "statuses": statuses,
            "stale_cutoff": stale_cutoff,
            "age_cutoff": age_cutoff,
            "claim_cutoff": claim_cutoff,
            "limit": limit,
        }
        if not include_failed:
            params["max_retries"] = MAX_AUTO_RETRIES
        if user_sub:
            params["user_sub"] = user_sub
        rows = db.execute(q, params).fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]
    finally:
        db.close()


def _try_claim(session_factory, audio_id: str) -> bool:
    """UPDATE atomique du lease watchdog. Retourne True si on a le claim.

    Race-safe : 2 pods qui scan simultanément verront la même row, mais
    un seul gagnera (la commit transaction). L'autre récupère rowcount=0.
    """
    db = session_factory()
    try:
        claim_cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_LEASE_S)
        now = datetime.now(timezone.utc)
        result = db.execute(sql_text("""
            UPDATE user_audio_files
               SET pipeline_claim_at = :now,
                   pipeline_claim_pod = :pod
             WHERE id = :audio_id
               AND (pipeline_claim_at IS NULL OR pipeline_claim_at < :claim_cutoff)
        """), {
            "now": now, "pod": _POD_HOSTNAME[:128],
            "audio_id": audio_id, "claim_cutoff": claim_cutoff,
        })
        db.commit()
        return result.rowcount > 0
    except Exception:
        db.rollback()
        logger.exception("watchdog claim failed for %s", audio_id)
        return False
    finally:
        db.close()


def resume_one(audio_id: str, user_sub: str, *, reason: str = "watchdog",
                session_factory=None) -> dict:
    """Reprise d'un job bloqué. Branche selon l'origine :

      - row 'mcr_import' SANS stored_filename → republier sur QUEUE_MCR_IMPORT
        (le worker mcr_importer ré-essaiera download audio puis fallback
        transcript + chaîne LLM)
      - autres rows → ``_reset_and_resubmit_kevent_pipeline`` (Whisper + LLM)
    """
    # Inspect d'abord la row pour décider du chemin.
    if session_factory is not None:
        try:
            from libs.shared.app.models import UserAudioFile
            db = session_factory()
            try:
                row = (
                    db.query(UserAudioFile)
                    .filter(UserAudioFile.id == audio_id,
                            UserAudioFile.user_sub == user_sub)
                    .first()
                )
                if row is not None and row.origin == "mcr_import" and not row.stored_filename:
                    return _resume_mcr_import(audio_id, user_sub, row, reason=reason)
            finally:
                db.close()
        except Exception:
            logger.exception("resume_one: inspection origin failed for %s, fallback kevent path", audio_id)

    # Default : pipeline kevent (audio + LLM).
    from app.puller import _reset_and_resubmit_kevent_pipeline
    code, payload = _reset_and_resubmit_kevent_pipeline(audio_id, user_sub, reason=reason)
    return {"audio_id": audio_id, "http_code": code, **payload}


def _resume_mcr_import(audio_id: str, user_sub: str, row, *, reason: str) -> dict:
    """Re-publie une row mcr_import sur QUEUE_MCR_IMPORT.

    Réinitialise le statut à 'mcr_import_pending' + last_activity_at, puis
    publie un message au worker. Le dédoublonnage côté worker (index partiel
    migration 019) garantit qu'on réutilisera la même row.
    """
    try:
        from libs.shared.app.config import RabbitMQConfig
        from libs.shared.app.queue_helper import QUEUE_MCR_IMPORT, publish_message
        message = {
            "user_audio_file_id": audio_id,
            "mcr_meeting_id": row.mcr_meeting_id,
            "user_sub": user_sub,
            "user_email": row.user_email or "",
            "fallback_transcript": True,
        }
        publish_message(RabbitMQConfig(), QUEUE_MCR_IMPORT, message)
        logger.info(
            "resume_one: re-published mcr_import row=%s mcr_meeting_id=%s reason=%s",
            audio_id, row.mcr_meeting_id, reason,
        )
        return {
            "audio_id": audio_id,
            "http_code": 202,
            "reprocessed": True,
            "mode": "mcr_import_republish",
            "mcr_meeting_id": row.mcr_meeting_id,
        }
    except Exception as exc:
        logger.exception("resume_one: mcr_import republish failed for %s", audio_id)
        return {"audio_id": audio_id, "http_code": 500, "error": str(exc)}


def scan_and_resume(session_factory, *, user_sub: Optional[str] = None,
                     limit: int = SCAN_BATCH_LIMIT,
                     include_failed: bool = False) -> dict:
    """Scan + claim + resume en un seul appel. Utilisé par le thread daemon
    ET par l'endpoint manuel ``/resume-stuck-jobs``.

    ``include_failed=True`` ajoute ``kevent_failed`` aux candidats — à
    utiliser depuis l'endpoint manuel (bouton "Relancer les bloqués"),
    pas depuis le tick automatique (sinon boucle de retry infinie sur
    un fichier vraiment cassé).

    Retourne ``{scanned, claimed, resumed: [details], skipped: int}``.
    """
    candidates = _scan_stuck(session_factory, user_sub=user_sub, limit=limit,
                              include_failed=include_failed)
    resumed = []
    skipped = 0
    for audio_id, owner_sub in candidates:
        if not _try_claim(session_factory, audio_id):
            skipped += 1
            continue
        try:
            r = resume_one(audio_id, owner_sub, reason="watchdog",
                            session_factory=session_factory)
            resumed.append(r)
        except Exception:
            logger.exception("watchdog: resume_one failed for %s", audio_id)
            skipped += 1
    return {
        "scanned": len(candidates),
        "claimed": len(resumed),
        "resumed": resumed,
        "skipped": skipped,
    }


def _mark_capped_as_failed(session_factory) -> int:
    """Marque kevent_failed les rows qui ont dépassé le cap de retentatives
    auto ET qui sont encore en état non-terminal stale. Sortir du
    purgatoire pour qu'elles apparaissent clairement comme failed côté UI.
    """
    db = session_factory()
    try:
        stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_THRESHOLD_S)
        result = db.execute(sql_text("""
            UPDATE user_audio_files
               SET transcription_status = 'kevent_failed',
                   transcription_completed_at = COALESCE(transcription_completed_at, NOW()),
                   last_activity_at = NOW(),
                   pipeline_claim_at = NULL,
                   pipeline_claim_pod = NULL
             WHERE transcription_status = ANY(:statuses)
               AND COALESCE(reprocess_version, 0) >= :max_retries
               AND (last_activity_at IS NULL OR last_activity_at < :stale_cutoff)
        """), {
            "statuses": list(NON_TERMINAL_STATUSES),
            "max_retries": MAX_AUTO_RETRIES,
            "stale_cutoff": stale_cutoff,
        })
        db.commit()
        return result.rowcount
    except Exception:
        db.rollback()
        logger.exception("watchdog: _mark_capped_as_failed failed")
        return 0
    finally:
        db.close()


def _watchdog_loop(session_factory):
    logger.info(
        "pipeline_watchdog started: interval=%ss stale=%ss lease=%ss pod=%s",
        WATCHDOG_INTERVAL_S, STALE_THRESHOLD_S, CLAIM_LEASE_S, _POD_HOSTNAME,
    )
    while True:
        try:
            time.sleep(WATCHDOG_INTERVAL_S)
            # 1) Sortir du purgatoire les rows qui ont dépassé le cap
            #    retries. Évite la boucle infinie sur un job vraiment cassé.
            capped = _mark_capped_as_failed(session_factory)
            if capped > 0:
                logger.warning(
                    "pipeline_watchdog: %d row(s) capped at %d retries → marked kevent_failed",
                    capped, MAX_AUTO_RETRIES,
                )
            # 2) Scan + claim + resume normal.
            result = scan_and_resume(session_factory)
            if result["claimed"] > 0 or result["skipped"] > 0:
                logger.info(
                    "pipeline_watchdog tick: scanned=%d claimed=%d skipped=%d",
                    result["scanned"], result["claimed"], result["skipped"],
                )
        except Exception:
            logger.exception("pipeline_watchdog tick crashed (continuing)")


def start_watchdog(session_factory):
    """Lance le thread daemon. Idempotent + opt-out via env."""
    global _watchdog_started
    if os.environ.get("PIPELINE_WATCHDOG_DISABLED") == "1":
        logger.info("pipeline_watchdog disabled via env")
        return
    with _watchdog_lock:
        if _watchdog_started:
            return
        _watchdog_started = True
    t = threading.Thread(
        target=_watchdog_loop,
        args=(session_factory,),
        daemon=True,
        name="pipeline-watchdog",
    )
    t.start()
