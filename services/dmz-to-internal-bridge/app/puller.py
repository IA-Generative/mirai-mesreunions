"""
File Puller Service (Zone Interne)
==================================
Entry point that integrates a transcoded file into the protected zone.

Two ways to learn that a new file is ready:
  1. Drain ``internal_pull`` queue periodically (every
     ``INTERNAL_PULL_QUEUE_INTERVAL_SECONDS``). The AMQP socket is opened
     **outbound** from inside the protected zone — no inbound connection
     ever crosses the boundary. This is the source of truth.
  2. ``/api/v1/pull-trigger`` HTTP endpoint exposed via Ingress: an
     optional, bearer-protected wake-up that lets dmz-to-internal-bridge ask "drain now"
     and reach near-zero latency. If anything blocks the trigger (ACL,
     network, token rotated), the polling tick still catches up.

Both paths converge on ``_perform_pull(payload)``, which does the actual
S3 download/upload, DB insert, and transcription enqueue. The function is
idempotent: calling it twice with the same ``(user_sub, simple_code,
transcoded_filename)`` tuple results in a single internal record.

The legacy ``/api/v1/pull`` route is preserved for backward compatibility
with deployments where in-cluster DNS still works (local docker-compose,
single-cluster integrations). It calls ``_perform_pull`` directly with the
HTTP body — same logic, just a different trigger.
"""

import ipaddress
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from io import BytesIO
from typing import Optional
from uuid import uuid4
from pathlib import Path

import requests as req
from flask import Flask, request, jsonify
from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    load_int_db, load_s3_processed, load_s3_internal,
    INTERNAL_API_TOKEN, RabbitMQConfig,
    INTERNAL_PULL_QUEUE_INTERVAL_SECONDS,
    TRANSCRIPTION_BACKEND, MCR_GATEWAY_URL, OIDC_TOKEN_ENDPOINT,
    KEVENT_GATEWAY_URL, KEVENT_API_KEY,
    KEVENT_TRANSCRIPTION_MODEL, KEVENT_DIARIZATION_MODEL,
    DIARIZATION_BACKEND, DIARIZATION_VM_URL,
    KEVENT_DIARIZATION_ENABLED, KEVENT_SPEAKER_NAMING_ENABLED,
    KEVENT_OOB_CLEANING_ENABLED, KEVENT_REFORMULATION_ENABLED,
    KEVENT_MEETING_ANALYSIS_ENABLED,
    KEVENT_GLOSSARY_CORRECTION_ENABLED, KEVENT_GLOSSARY_DIR,
    KEVENT_GLOSSARY_MAX_TERMS_PER_CALL,
    KEVENT_FILENAME_SUGGESTION_ENABLED,
    KEVENT_ABSENTEE_SUMMARY_ENABLED,
    KEVENT_ASYNC_MODE, KEVENT_ASYNC_SERVICE_TYPE,
    KEVENT_ASYNC_TRANSCRIPTION_OPERATION, KEVENT_ASYNC_DIARIZATION_OPERATION,
    KEVENT_ASYNC_POLL_INTERVAL_SECONDS, KEVENT_ASYNC_TIMEOUT_SECONDS,
    KEVENT_HTTP_TIMEOUT_SECONDS, KEVENT_DIARIZATION_FORMAT,
    TRANSCODE_SAMPLE_RATE, TRANSCODE_CHANNELS,
    LITELLM_BASE_URL, LITELLM_API_KEY, LLM_HTTP_TIMEOUT_SECONDS,
    LLM_MODEL_SMALL, LLM_MODEL_MEDIUM, LLM_MODEL_LARGE,
)
from libs.shared.app.models import InternalBase, UserAudioFile, Preparation, Meeting
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.s3_helper import download_fileobj, upload_fileobj, ensure_bucket, delete_object
from libs.shared.app.queue_helper import (
    publish_message,
    declare_queues,
    drain_queue_once,
    QUEUE_TRANSCRIPTION,
    QUEUE_INTERNAL_PULL,
)
from libs.shared.app.security import require_strong_shared_secret, verify_bearer_token
from libs.shared.app.secrets_crypto import decrypt as decrypt_secret
from libs.shared.app.oidc_refresh_store import fetch_ciphertext, delete_ciphertext
from app.mcr_client import (
    MCRClient,
    MCRAuthError,
    MCRTransientError,
    MCRApplicativeError,
)
from app.kevent_client import (
    KeventClient,
    KeventAuthError,
    KeventTransientError,
    KeventApplicativeError,
)
from app.llm_client import LLMClient
from app.diarization_merger import merge_to_markdown
from app import meeting_intelligence as mi
from app.glossary_loader import load_glossary_dir

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


# Static general glossary loaded once per worker process. Reload requires a pod
# restart — acceptable since the glossary lives in the image (cf
# `docs/integrate-with-kevent.md` § Glossaire).
_GLOSSARY_TERMS: list[str] = []
if KEVENT_GLOSSARY_CORRECTION_ENABLED:
    _GLOSSARY_TERMS = load_glossary_dir(KEVENT_GLOSSARY_DIR)
    logger.info("Loaded %d glossary terms from %s", len(_GLOSSARY_TERMS), KEVENT_GLOSSARY_DIR)

app = Flask(__name__)

db_cfg = load_int_db()
s3_processed_cfg = load_s3_processed()
s3_internal_cfg = load_s3_internal()
rabbit_cfg = RabbitMQConfig()
SessionLocal = None
_purge_thread_started = False
_pull_loop_thread_started = False
_orphan_resume_started = False
_orphan_watchdog_started = False
# États de transcription "non terminaux" — déclenchent une reprise du
# poll Kevent si le pod redémarre alors qu'un fichier est dans cet état.
_POLLING_STATES = (
    "kevent_queued", "kevent_transcribing", "kevent_processing",
    "pending", "processing",
)
# Au-delà de cette fenêtre depuis création, un job orphelin est considéré
# perdu (gateway Kevent purge ses jobs après TTL court — leur résultat
# est inaccessible). Inutile de tenter une reprise.
_ORPHAN_MAX_AGE_HOURS = 24
_ORPHAN_WATCHDOG_INTERVAL_S = 300  # 5 min

INTERNAL_PURGE_INTERVAL_SECONDS = max(60, int(os.getenv("INTERNAL_PURGE_INTERVAL_SECONDS", "86400")))
INTERNAL_PURGE_MAX_AGE_DAYS = max(1, int(os.getenv("INTERNAL_PURGE_MAX_AGE_DAYS", "7")))
INTERNAL_PURGE_LOCK_ID = int(os.getenv("INTERNAL_PURGE_LOCK_ID", "910019001"))

EXTERNAL_CALLBACK_URL = os.getenv(
    "EXTERNAL_CALLBACK_URL", "http://mobile-upload-pwa:8081/api/notify-status"
)

INTERNAL_PUSH_TRIGGER_TOKEN = os.getenv("INTERNAL_PUSH_TRIGGER_TOKEN", "")
_TRIGGER_IP_ALLOWLIST_RAW = os.getenv("INTERNAL_PUSH_TRIGGER_IP_ALLOWLIST", "")


def _parse_ip_allowlist(raw: str):
    """Return a list of ip_network objects, ignoring blank entries."""
    nets = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid CIDR in trigger allowlist: %s", token)
    return nets


_TRIGGER_ALLOWED_NETS = _parse_ip_allowlist(_TRIGGER_IP_ALLOWLIST_RAW)


def _client_ip_allowed(remote_addr: str) -> bool:
    """Empty allowlist => allow (rely on nginx whitelist + bearer)."""
    if not _TRIGGER_ALLOWED_NETS:
        return True
    try:
        client = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(client in net for net in _TRIGGER_ALLOWED_NETS)


# Cache mémoire (TTL 5s) pour /api/v1/queue-status — évite de marteler la
# gateway si plusieurs onglets PWA/mydevices polll en parallèle.
import threading as _qs_threading
import time as _qs_time
_QS_CACHE_TTL = 5.0
_qs_cache: dict = {}  # {service_type: (monotonic_ts, payload_or_none)}
_qs_lock = _qs_threading.Lock()


def _queue_status_cache_get_or_fetch(service_type: str):
    """Renvoie le dict ``payload`` (réponse gateway /jobs) ou None si la
    gateway est injoignable. Cache TTL 5s par service_type."""
    now = _qs_time.monotonic()
    with _qs_lock:
        entry = _qs_cache.get(service_type)
        if entry and (now - entry[0]) < _QS_CACHE_TTL:
            return entry[1]
    client = _build_kevent_client()
    if client is None:
        return None
    try:
        payload = client.list_jobs(service_type=service_type, limit=50)
    except Exception:
        logger.warning("queue-status: gateway list_jobs failed", exc_info=True)
        with _qs_lock:
            _qs_cache[service_type] = (now, None)
        return None
    with _qs_lock:
        _qs_cache[service_type] = (now, payload)
    return payload


def notify_external_status(file_id: str, status: str, message: str, timeout: int = 5) -> None:
    """Push transfer progression/status back to external portal."""
    try:
        resp = req.post(
            EXTERNAL_CALLBACK_URL,
            json={
                "file_id": file_id,
                "status": status,
                "message": message,
            },
            headers={
                "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Failed to callback external zone (%s): %s", status, e)


def verify_token():
    auth = request.headers.get("Authorization", "")
    return verify_bearer_token(auth, INTERNAL_API_TOKEN)


def verify_trigger_token():
    """Bearer for the new /api/v1/pull-trigger route — distinct from INTERNAL_API_TOKEN."""
    auth = request.headers.get("Authorization", "")
    return bool(INTERNAL_PUSH_TRIGGER_TOKEN) and verify_bearer_token(auth, INTERNAL_PUSH_TRIGGER_TOKEN)


def _guess_audio_mime(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext == ".mp4":
        return "audio/mp4"
    if ext == ".m4a":
        return "audio/mp4"
    if ext == ".wav":
        return "audio/wav"
    if ext == ".ogg":
        return "audio/ogg"
    return "application/octet-stream"


def _build_mcr_client() -> Optional[MCRClient]:
    """Construct the MCR client lazily, returning None if disabled or misconfigured."""
    if TRANSCRIPTION_BACKEND != "mcr":
        return None
    try:
        return MCRClient(
            gateway_url=MCR_GATEWAY_URL,
            oidc_token_endpoint=OIDC_TOKEN_ENDPOINT,
            oidc_client_id=os.getenv("OIDC_CLIENT_ID", ""),
            oidc_client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
        )
    except ValueError:
        logger.exception(
            "TRANSCRIPTION_BACKEND=mcr but client config is incomplete; falling back to local transcription queue"
        )
        return None


def _build_kevent_client() -> Optional[KeventClient]:
    """Construct the Kevent client lazily for the kevent backend."""
    if TRANSCRIPTION_BACKEND != "kevent":
        return None
    try:
        return KeventClient(
            gateway_url=KEVENT_GATEWAY_URL,
            api_key=KEVENT_API_KEY,
            transcription_model=KEVENT_TRANSCRIPTION_MODEL,
            diarization_model=KEVENT_DIARIZATION_MODEL,
            timeout=KEVENT_HTTP_TIMEOUT_SECONDS,
            diarization_backend=DIARIZATION_BACKEND,
            diarization_vm_url=DIARIZATION_VM_URL,
        )
    except ValueError:
        logger.exception(
            "TRANSCRIPTION_BACKEND=kevent but Kevent config is incomplete; falling back to local transcription queue"
        )
        return None


def _build_llm_client() -> Optional[LLMClient]:
    """Construct the LiteLLM client used by the kevent meeting-intelligence steps."""
    try:
        return LLMClient(
            base_url=LITELLM_BASE_URL,
            api_key=LITELLM_API_KEY,
            timeout=LLM_HTTP_TIMEOUT_SECONDS,
        )
    except ValueError:
        logger.warning(
            "LiteLLM config missing (LITELLM_BASE_URL or LITELLM_API_KEY); meeting-intelligence steps disabled"
        )
        return None


def _set_user_audio_status(audio_file_id, status: str, **fields) -> None:
    """
    Update transcription_status and any other named columns on a UserAudioFile row.

    Optional kwargs accepted: ``mcr_meeting_id``, ``transcription_engine``,
    ``transcription_text``, ``transcription_language``, ``diarization_json``,
    ``speaker_tagged_text``, ``glossary_corrected_text``, ``cleaned_text``,
    ``reformulated_text``, ``meeting_analysis_json``,
    ``suggested_filename``, ``key_points_summary``.
    """
    allowed = {
        "mcr_meeting_id",
        "transcription_engine",
        "transcription_text",
        "transcription_language",
        "diarization_json",
        "speaker_tagged_text",
        "glossary_corrected_text",
        "cleaned_text",
        "reformulated_text",
        "meeting_analysis_json",
        "absentee_summary",
        "suggested_filename",
        "key_points_summary",
        "kevent_job_id",
        # meeting-prep v2 — reprocess tracking (noms canoniques migration 012)
        "meeting_id",
        "reprocess_version",
        "reprocessed_with_meeting_id",
        "last_reprocessed_at",
        "reprocess_history",
    }
    db = SessionLocal()
    try:
        rec = db.query(UserAudioFile).filter(UserAudioFile.id == audio_file_id).first()
        if rec is None:
            logger.warning("UserAudioFile not found for status update: %s", audio_file_id)
            return
        rec.transcription_status = status
        for key, val in fields.items():
            if key in allowed:
                setattr(rec, key, val)
        db.commit()
    finally:
        db.close()


from app.audio_format import to_diarization_format as _to_diarization_format


# ── Phase 2bis : reprise auto des polls Kevent orphelins ─────────────────
#
# Quand un pod internal-ingester meurt (OOM, scale-down, rollout) pendant qu'il
# poll Kevent pour un fichier, le job continue côté gateway mais plus
# personne ne récupère le résultat. Le fichier reste figé dans son état
# de polling (kevent_queued / kevent_transcribing / kevent_processing).
#
# Avec kevent_job_id persisté, on peut au boot scanner ces rows et relancer
# un `client.resume_job(...)` sans re-uploader. Idempotent : si le job est
# encore en cours, on le poll ; s'il est terminé, on récupère le résultat ;
# s'il a été TTL'd côté Kevent (>24h), on marque kevent_failed pour libérer
# la row.

def _resume_orphaned_kevent_polls() -> None:
    """Au boot, repère les fichiers dont le poll Kevent a été interrompu et
    relance le poll dans un thread par row. Best-effort : log et continue
    en cas d'erreur de scan ou de reprise. Borné à 24h pour éviter de
    tenter de poller des jobs déjà TTL'd côté gateway."""
    if SessionLocal is None:
        logger.debug("orphan resume: SessionLocal not ready, skipping")
        return
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_ORPHAN_MAX_AGE_HOURS)
        db = SessionLocal()
        try:
            rows = (
                db.query(UserAudioFile)
                .filter(
                    UserAudioFile.transcription_status.in_(_POLLING_STATES),
                    UserAudioFile.kevent_job_id.isnot(None),
                    UserAudioFile.created_at > cutoff,
                )
                .all()
            )
            # Détacher avant fermeture de session pour bosser sans
            # référence à la session DB dans les threads enfants.
            snapshot = [(str(r.id), r.kevent_job_id) for r in rows]
        finally:
            db.close()
        if not snapshot:
            logger.info("orphan resume: 0 orphaned kevent polls to resume")
            return
        logger.info("orphan resume: resuming %d orphaned kevent poll(s) at boot", len(snapshot))
        for audio_id, job_id in snapshot:
            threading.Thread(
                target=_resume_one_kevent_poll,
                args=(audio_id, job_id),
                daemon=True,
                name=f"kevent-resume-{audio_id[:8]}",
            ).start()
    except Exception:
        logger.exception("orphan resume scan failed (non-fatal)")


def _resume_one_kevent_poll(audio_file_id: str, job_id: str) -> None:
    """Diagnostic d'un poll Kevent orphelin (pod précédent mort en cours).

    ⚠ Première version : on NE marque PAS kevent_failed agressivement,
    parce qu'on n'a pas (encore) le code pour ré-engager le pipeline
    downstream (diarization → LLM → CR) à partir d'un résultat Whisper
    obtenu post-resume. Marquer failed = l'utilisateur croit que la
    transcription a échoué alors qu'elle est peut-être dispo côté Kevent.

    Comportement pour l'instant :
    - Si le job est TTL'd (404) → log WARNING, status reste tel quel.
      L'UI restera en kevent_queued/processing jusqu'à ce que le user
      ré-upload ou que le watchdog suivant le déclare definitive.
    - Si le job est encore en cours → log INFO, status inchangé.
      Le user n'aura pas de résultat mais ne verra pas "failed" non plus.
    - Si erreur transitoire → log warning, status inchangé.

    À itérer : refactor _transcribe_via_kevent en sous-fonctions composables
    pour que cette reprise puisse continuer le pipeline depuis n'importe
    quelle étape. Tracking : voir issue #TODO.
    """
    client = _build_kevent_client()
    if client is None:
        logger.warning("orphan resume %s: kevent client unavailable", audio_file_id)
        return
    try:
        logger.info("orphan resume %s: probing kevent job_id=%s", audio_file_id, job_id)
        try:
            # On utilise get_job (1 fetch) plutôt que wait_for_job (boucle
            # de poll bloquante) pour juste diagnostiquer l'état actuel
            # sans s'engager dans une attente longue qu'on ne peut pas
            # consommer en aval.
            info = client.get_job(KEVENT_ASYNC_SERVICE_TYPE, job_id)
            status = (info.get("status") or "").lower()
            logger.warning(
                "orphan resume %s: kevent job %s status=%s — pipeline downstream non re-engagé. "
                "Statut DB inchangé, l'utilisateur doit ré-uploader si nécessaire.",
                audio_file_id, job_id, status,
            )
        except KeventApplicativeError as exc:
            msg = str(exc)
            if "not found" in msg.lower() or "404" in msg:
                logger.warning(
                    "orphan resume %s: kevent job %s TTL'd (404) — statut DB inchangé, "
                    "l'utilisateur doit ré-uploader.",
                    audio_file_id, job_id,
                )
            else:
                logger.exception("orphan resume %s: applicative error on %s",
                                 audio_file_id, job_id)
        except KeventTransientError:
            logger.warning(
                "orphan resume %s: transient error on %s — retry via watchdog",
                audio_file_id, job_id,
            )
    except Exception:
        logger.exception("orphan resume %s: unexpected error", audio_file_id)


def _orphan_watchdog_loop() -> None:
    """Toutes les 5 min, scanne les rows en polling state qui :
      - n'ont pas de kevent_job_id (race window submission → DB write impossible
        à reprendre → terminal), OU
      - ont un kevent_job_id mais pas d'update depuis > 30 min (signe que
        le poll thread est mort silencieusement)."""
    while True:
        time.sleep(_ORPHAN_WATCHDOG_INTERVAL_S)
        if SessionLocal is None:
            continue
        try:
            cutoff_create = datetime.now(timezone.utc) - timedelta(hours=_ORPHAN_MAX_AGE_HOURS)
            cutoff_update = datetime.now(timezone.utc) - timedelta(minutes=30)
            db = SessionLocal()
            try:
                from sqlalchemy import or_, and_
                rows = (
                    db.query(UserAudioFile)
                    .filter(
                        UserAudioFile.transcription_status.in_(_POLLING_STATES),
                        UserAudioFile.created_at > cutoff_create,
                        or_(
                            UserAudioFile.kevent_job_id.is_(None),
                            and_(
                                UserAudioFile.kevent_job_id.isnot(None),
                                UserAudioFile.transcription_started_at < cutoff_update,
                            ),
                        ),
                    )
                    .all()
                )
                snapshot = [(str(r.id), r.kevent_job_id) for r in rows]
            finally:
                db.close()
            if not snapshot:
                continue
            logger.info("orphan watchdog: %d stuck row(s) detected", len(snapshot))
            for audio_id, job_id in snapshot:
                if job_id:
                    threading.Thread(
                        target=_resume_one_kevent_poll,
                        args=(audio_id, job_id),
                        daemon=True,
                        name=f"kevent-watchdog-{audio_id[:8]}",
                    ).start()
                else:
                    # Pas de job_id → impossible à reprendre. Terminal.
                    _set_user_audio_status(
                        audio_id, "kevent_failed",
                        transcription_engine="kevent",
                    )
                    logger.warning("orphan watchdog: row %s has no kevent_job_id, marked failed", audio_id)
        except Exception:
            logger.exception("orphan watchdog scan failed (non-fatal, retry in %ss)",
                             _ORPHAN_WATCHDOG_INTERVAL_S)


def _run_llm_chain_for_audio(
    base_for_llm: str,
    speaker_tagged: Optional[str],
    *,
    llm,
    glossary_terms: Optional[list[str]] = None,
    include_metadata: bool = True,
) -> tuple[dict, str]:
    """Exécute la chaîne LLM post-transcription (steps 3b-bis → 3f).

    Sous-fonction composable extraite de ``_transcribe_via_kevent`` (cf
    refactor meeting-prep v2 §A) pour être réutilisée par
    ``POST /api/v1/audio/<id>/reprocess`` (cf §5.2 du plan).

    Paramètres :
      - ``base_for_llm`` : texte de départ (speaker_tagged ou transcription).
      - ``speaker_tagged`` : texte taggé par locuteur (si dispo) ; sert au
        ``meeting_analysis`` pour distinguer participants présents vs cités.
      - ``glossary_terms`` : liste explicite de termes à passer au
        glossary_correction. Si ``None``, fallback sur ``_GLOSSARY_TERMS``
        (statique chargé au boot). Le caller peut fusionner statique +
        brief + user-glossary.
      - ``include_metadata`` : True pour le run initial (suggéré_filename,
        key_points). False pour les reprocess (on garde l'existant).

    Retourne ``(updates, status_delta)`` où :
      - ``updates`` est un dict de colonnes à écrire (clé → valeur).
      - ``status_delta`` est ``"kevent_completed"`` (succès) ou
        ``"kevent_partially_completed"`` (au moins une étape obligatoire
        a renvoyé None).
    """
    updates: dict = {}
    status_delta = "kevent_completed"
    glossary = list(glossary_terms) if glossary_terms is not None else list(_GLOSSARY_TERMS)

    if llm is None:
        # Pas de LiteLLM configuré → on sort sans tenter aucune étape.
        return updates, status_delta

    # 3b-bis. Glossary correction.
    if KEVENT_GLOSSARY_CORRECTION_ENABLED and glossary:
        corrected = mi.apply_glossary_correction(
            base_for_llm, llm, LLM_MODEL_MEDIUM,
            glossary_terms=glossary,
            max_terms_per_call=KEVENT_GLOSSARY_MAX_TERMS_PER_CALL,
        )
        if corrected:
            updates["glossary_corrected_text"] = corrected
            base_for_llm = corrected

    # 3b-ter. Suggested filename + key points (1er run uniquement).
    if include_metadata and KEVENT_FILENAME_SUGGESTION_ENABLED:
        meta = mi.suggest_metadata(base_for_llm, llm, LLM_MODEL_SMALL)
        if meta:
            if meta.get("title"):
                updates["suggested_filename"] = meta["title"]
            kp_serialized = mi.serialize_key_points(meta.get("key_points") or [])
            if kp_serialized:
                updates["key_points_summary"] = kp_serialized

    # 3c. OOB cleaning.
    if KEVENT_OOB_CLEANING_ENABLED:
        cleaned = mi.clean_oob(base_for_llm, llm, LLM_MODEL_MEDIUM)
        if cleaned:
            updates["cleaned_text"] = cleaned
        else:
            status_delta = "kevent_partially_completed"

    # 3d. Reformulation.
    if KEVENT_REFORMULATION_ENABLED:
        source = updates.get("cleaned_text") or base_for_llm
        reformulated = mi.reformulate(source, llm, LLM_MODEL_MEDIUM)
        if reformulated:
            updates["reformulated_text"] = reformulated
        else:
            status_delta = "kevent_partially_completed"

    # 3e. Meeting analysis.
    if KEVENT_MEETING_ANALYSIS_ENABLED:
        source = updates.get("cleaned_text") or base_for_llm
        analysis = mi.analyse_meeting(
            source, llm, LLM_MODEL_LARGE,
            speaker_tagged_text=speaker_tagged,
        )
        serialized = mi.serialize_analysis(analysis)
        if serialized is not None:
            updates["meeting_analysis_json"] = serialized
        else:
            status_delta = "kevent_partially_completed"

    # 3f. Absentee summary.
    if KEVENT_ABSENTEE_SUMMARY_ENABLED:
        source = updates.get("cleaned_text") or base_for_llm
        summary = mi.summarise_for_absentee(source, llm, LLM_MODEL_MEDIUM)
        if summary:
            updates["absentee_summary"] = summary
        else:
            status_delta = "kevent_partially_completed"

    return updates, status_delta


def _fetch_preparation_glossary_terms(preparation_id, db) -> tuple[list[str], Optional[str]]:
    """Charge la Preparation par id et retourne ``(glossary_terms, initial_prompt)``.

    ``glossary_terms`` est la liste extraite via
    ``extract_full_glossary_terms_from_brief()`` (cap 200, le helper consomme
    le dict ``content`` ex-``brief_json``).
    ``initial_prompt`` est la phrase Whisper via
    ``extract_whisper_initial_prompt()`` (cap 50 termes / 200 tokens).

    Best-effort : tout échec → ``([], None)`` + warning loggé.
    """
    if not preparation_id:
        return [], None
    try:
        from app.glossary_from_brief import (
            extract_full_glossary_terms_from_brief,
            extract_whisper_initial_prompt,
        )
        prep = db.query(Preparation).filter(Preparation.id == preparation_id).first()
        if prep is None:
            return [], None
        # Migration 012 : Preparation.content remplace MeetingBrief.brief_json.
        content = prep.content or {}
        documents = prep.documents or []
        terms = sorted(extract_full_glossary_terms_from_brief(content, documents))
        initial = extract_whisper_initial_prompt(content, documents) or None
        return terms, initial
    except Exception:
        logger.exception("fetch_preparation_glossary_terms failed for prep=%s", preparation_id)
        return [], None


# Alias rétrocompat interne : conserve l'ancien nom pour ne pas casser
# d'éventuels imports tiers ; pointe vers la nouvelle fonction.
_fetch_brief_glossary_terms = _fetch_preparation_glossary_terms


def _maybe_trigger_cr_email_for_audio(audio_file_id, payload: dict) -> None:
    """Lot 8 — Hook post-transcription : trigger envoi CR par email.

    Logique :
      1. Charge le Meeting lié à cet ``audio_file_id`` (via UAF.meeting_id ou
         Meeting.user_audio_file_id).
      2. Charge la Preparation liée au Meeting (si présente).
      3. Vérifie ``preparation.send_cr_email == True`` et au moins un
         participant avec un email.
      4. POST ``MESREUNIONS_WEB_INTERNAL_BASE_URL/api/meetings/<id>/send-cr``
         avec ``Authorization: Bearer INTERNAL_API_TOKEN`` + body
         ``{user_sub}``. mesreunions-web s'occupe du SMTP.

    Best-effort : tout échec → log + return (jamais d'exception remontée).
    Désactivable via env ``MEETING_CR_EMAIL_HOOK_ENABLED=false``.
    """
    if os.getenv("MEETING_CR_EMAIL_HOOK_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return
    base = (os.getenv("MESREUNIONS_WEB_INTERNAL_BASE_URL") or "").rstrip("/")
    if not base:
        logger.debug("send-cr hook: MESREUNIONS_WEB_INTERNAL_BASE_URL not set, skipping")
        return
    if SessionLocal is None:
        return
    db = SessionLocal()
    try:
        # Recherche du meeting lié.
        meeting = (
            db.query(Meeting)
            .filter(Meeting.user_audio_file_id == audio_file_id, Meeting.trashed_at.is_(None))
            .first()
        )
        if meeting is None:
            uaf = (
                db.query(UserAudioFile)
                .filter(UserAudioFile.id == audio_file_id)
                .first()
            )
            if uaf and getattr(uaf, "meeting_id", None):
                meeting = (
                    db.query(Meeting)
                    .filter(Meeting.id == uaf.meeting_id, Meeting.trashed_at.is_(None))
                    .first()
                )
        if meeting is None:
            return
        if not meeting.preparation_id:
            return
        prep = (
            db.query(Preparation)
            .filter(Preparation.id == meeting.preparation_id, Preparation.trashed_at.is_(None))
            .first()
        )
        if prep is None or not bool(getattr(prep, "send_cr_email", False)):
            return
        participants = prep.participants or []
        emails = [
            (p.get("email") or "").strip()
            for p in participants
            if isinstance(p, dict) and p.get("email") and "@" in (p.get("email") or "")
        ]
        if not emails:
            logger.info(
                "send-cr hook: prep=%s opted-in but no participant email — skip",
                prep.id,
            )
            return
        user_sub = (payload or {}).get("user_sub") or meeting.user_sub
        url = f"{base}/api/meetings/{meeting.id}/send-cr"
        try:
            resp = req.post(
                url,
                json={"user_sub": user_sub},
                headers={"Authorization": f"Bearer {INTERNAL_API_TOKEN}"},
                timeout=15,
            )
            logger.info(
                "send-cr hook: meeting=%s prep=%s status=%s",
                meeting.id, prep.id, resp.status_code,
            )
        except Exception:
            logger.exception(
                "send-cr hook: HTTP call failed meeting=%s", meeting.id,
            )
    finally:
        db.close()


def _transcribe_via_kevent(audio_file_id, transcoded_filename: str,
                            file_data, payload: dict) -> None:
    """
    Run the full Kevent pipeline:
      1. transcribe via Kevent /v1/audio/transcriptions (always)
      2. optionally diarize, merge into speaker_tagged_text
      3. optionally LLM speaker naming, OOB cleaning, reformulation, analysis

    All post-transcription steps are best-effort: a failure leaves the
    corresponding column NULL but the raw transcription still ships.

    Raises ``KeventTransientError`` when the *transcription* itself fails
    transiently — caller (queue consumer) retries via the x-retry-count
    counter. Auth/applicative errors are caught and mapped to terminal
    statuses (kevent_failed) so the file isn't retried indefinitely.
    """
    client = _build_kevent_client()
    if client is None:
        _set_user_audio_status(audio_file_id, "kevent_failed",
                               transcription_engine="kevent")
        return

    # Read the audio bytes once, reuse for both transcribe + diarize.
    file_data.seek(0)
    audio_bytes = file_data.read()
    content_type = "audio/mp4"  # transcoded files are always .mp4

    async_timeout = KEVENT_ASYNC_TIMEOUT_SECONDS or float(KEVENT_HTTP_TIMEOUT_SECONDS)

    def _on_kevent_status(kevent_status: str):
        """Map kevent job statuses → our DB transcription_status so the
        mydevices UI can show 'queued' / 'processing' while polling.

        Also pushes a callback to mobile-upload-pwa so the mobile PWA can show
        the progress in real time (same WebSocket channel as the upload
        phase)."""
        mapped = {
            "pending": "kevent_queued",
            "processing": "kevent_processing",
        }.get(kevent_status)
        if mapped:
            try:
                _set_user_audio_status(audio_file_id, mapped,
                                       transcription_engine="kevent")
            except Exception:
                logger.exception("Failed to push intermediate status %s", mapped)
            # Best-effort callback vers mobile-upload-pwa pour le PWA mobile.
            external_file_id = (payload or {}).get("file_id") or str(audio_file_id)
            try:
                notify_external_status(
                    str(external_file_id),
                    mapped,
                    f"Transcription Kevent — {kevent_status}",
                )
            except Exception:
                pass  # déjà loggé dans notify_external_status

    def _on_kevent_submitted(job_id: str):
        """Persiste le kevent_job_id en DB dès que le gateway accepte le job.
        Sert à reprise post-restart (Phase 2bis) et à la position d'attente
        précise dans l'UI mydevices."""
        try:
            _set_user_audio_status(audio_file_id, "kevent_queued",
                                   transcription_engine="kevent",
                                   kevent_job_id=job_id)
        except Exception:
            logger.exception("Failed to persist kevent_job_id=%s for %s",
                             job_id, audio_file_id)

    # meeting-prep v2 §5.1bis (PR2d) : si une Meeting liée à cet audio porte
    # une preparation_id (auto-link en amont dans _perform_pull), on extrait
    # un mini-glossaire ciblé (≤50 termes ≈ 200 tokens) depuis la Preparation
    # et on le passe en ``initial_prompt`` Whisper. Best-effort : si la
    # lookup ou l'extraction échoue, on tombe en transcription nominale.
    initial_prompt_for_whisper: Optional[str] = None
    brief_glossary_terms_for_llm: list[str] = []
    try:
        if SessionLocal is not None:
            _db_probe = SessionLocal()
            try:
                m = (
                    _db_probe.query(Meeting)
                    .filter(
                        Meeting.user_audio_file_id == audio_file_id,
                        Meeting.trashed_at.is_(None),
                    )
                    .first()
                )
                preparation_id = m.preparation_id if (m and m.preparation_id) else None
                if preparation_id:
                    terms, prompt = _fetch_preparation_glossary_terms(preparation_id, _db_probe)
                    brief_glossary_terms_for_llm = terms
                    initial_prompt_for_whisper = prompt
                    if prompt:
                        logger.info(
                            "kevent: initial_prompt=%d chars for preparation=%s (audio=%s)",
                            len(prompt), preparation_id, audio_file_id,
                        )
            finally:
                _db_probe.close()
    except Exception:
        logger.exception("kevent: failed to load preparation glossary for audio=%s", audio_file_id)

    def _kevent_transcribe():
        if KEVENT_ASYNC_MODE:
            return client.transcribe_async(
                audio_bytes=audio_bytes,
                filename=transcoded_filename,
                content_type=content_type,
                service_type=KEVENT_ASYNC_SERVICE_TYPE,
                operation=KEVENT_ASYNC_TRANSCRIPTION_OPERATION,
                poll_interval=KEVENT_ASYNC_POLL_INTERVAL_SECONDS,
                timeout=async_timeout,
                on_status=_on_kevent_status,
                on_submitted=_on_kevent_submitted,
                initial_prompt=initial_prompt_for_whisper,
            )
        return client.transcribe(
            audio_bytes=audio_bytes,
            filename=transcoded_filename,
            content_type=content_type,
        )

    def _kevent_diarize():
        # Le format soumis à la diarisation est gouverné par
        # KEVENT_DIARIZATION_FORMAT (flac/wav/mp4). FLAC par défaut pour
        # éviter le bug "samples mismatch" de pyannote sur du MP4/AAC.
        # En cas de target="mp4" ou de ré-encodage raté, on retombe sur
        # les bytes originaux (kill-switch).
        d_bytes, d_filename, d_ctype = _to_diarization_format(
            audio_bytes, transcoded_filename, KEVENT_DIARIZATION_FORMAT,
            sample_rate=TRANSCODE_SAMPLE_RATE, channels=TRANSCODE_CHANNELS,
        )
        if KEVENT_ASYNC_MODE:
            return client.diarize_async(
                audio_bytes=d_bytes,
                filename=d_filename,
                content_type=d_ctype,
                service_type=KEVENT_ASYNC_SERVICE_TYPE,
                operation=KEVENT_ASYNC_DIARIZATION_OPERATION,
                poll_interval=KEVENT_ASYNC_POLL_INTERVAL_SECONDS,
                timeout=async_timeout,
                on_status=_on_kevent_status,
                on_submitted=_on_kevent_submitted,
            )
        return client.diarize(
            audio_bytes=d_bytes,
            filename=d_filename,
            content_type=d_ctype,
        )

    # ── Step 1 — transcription (always) ────────────────────────────────
    # On capture les erreurs Kevent et on les propage à la PWA via
    # notify_external_status pour que l'utilisateur ait un message clair
    # (sinon le fichier reste affiché "transferred" indéfiniment côté
    # mobile alors que la transcription a silencieusement échoué).
    external_file_id = (payload or {}).get("file_id") or str(audio_file_id)

    def _push_failure(status: str, message: str):
        try:
            _set_user_audio_status(audio_file_id, status,
                                   transcription_engine="kevent")
        except Exception:
            logger.exception("Failed to persist %s status for %s",
                             status, audio_file_id)
        try:
            notify_external_status(str(external_file_id), status, message)
        except Exception:
            pass  # déjà loggé dans notify_external_status

    try:
        transcription = _kevent_transcribe()
    except KeventAuthError:
        logger.exception("Kevent auth error on transcription for %s", audio_file_id)
        _push_failure(
            "kevent_failed",
            "Accès au backend IA refusé. La transcription automatique n'a "
            "pas pu démarrer — contactez un administrateur.",
        )
        return
    except KeventApplicativeError as exc:
        logger.exception("Kevent applicative error on transcription for %s", audio_file_id)
        _push_failure(
            "kevent_failed",
            "Erreur du backend IA pendant la transcription. Réessayez "
            "ultérieurement ou contactez un administrateur.",
        )
        return
    # KeventTransientError propagates → queue retry handles it.

    # Le champ "text" de Whisper est un seul long string sans \n, peu
    # lisible. On reconstruit à partir de "segments" (découpage naturel
    # par pauses/phrases, granularité ~10-30s) en mettant un saut de
    # ligne par segment. Fallback sur "text" si segments est vide ou
    # absent (response_format=json simple sans verbose).
    segments = transcription.get("segments") or []
    if segments:
        text = "\n".join(
            (seg.get("text") or "").strip()
            for seg in segments
            if (seg.get("text") or "").strip()
        ).strip()
    else:
        text = (transcription.get("text") or "").strip()
    language = transcription.get("language") or None
    logger.info(
        "Kevent transcribe: %d chars, %d segments, language=%s, audio_file_id=%s",
        len(text), len(segments), language, audio_file_id,
    )

    # We'll accumulate DB updates and apply them in one go at the end.
    updates: dict = {
        "transcription_engine": "kevent",
        "transcription_text": text,
        "transcription_language": language,
    }
    final_status = "kevent_completed"

    # ── Step 2 — diarisation (optional) + merge to markdown ────────────
    diarization: Optional[dict] = None
    if KEVENT_DIARIZATION_ENABLED:
        try:
            diarization = _kevent_diarize()
            logger.info(
                "Kevent diarize: %d segments, num_speakers=%s",
                len(diarization.get("segments") or []),
                diarization.get("num_speakers"),
            )
            updates["diarization_json"] = json.dumps(diarization, ensure_ascii=False)
        except (KeventAuthError, KeventApplicativeError):
            logger.exception("Kevent diarisation failed (non-retryable), continuing")
            final_status = "kevent_partially_completed"
        except KeventTransientError:
            logger.warning("Kevent diarisation transient failure, continuing without diarisation")
            final_status = "kevent_partially_completed"

    # ── Step 3 — LLM-based steps (all optional, best-effort) ───────────
    llm = _build_llm_client()
    speaker_tagged: Optional[str] = None

    # 3a. Build the speaker-tagged markdown if we have diarization.
    if diarization is not None:
        try:
            speaker_tagged = merge_to_markdown(transcription, diarization)
        except Exception:
            logger.exception("diarization merger failed, falling back to plain text")
            speaker_tagged = None

        # 3b. Speaker naming (small model) — replaces SPEAKER_NN with real names.
        if speaker_tagged and KEVENT_SPEAKER_NAMING_ENABLED and llm is not None:
            names = mi.extract_speaker_names(speaker_tagged, llm, LLM_MODEL_SMALL)
            if names:
                try:
                    speaker_tagged = merge_to_markdown(transcription, diarization, speaker_names=names)
                except Exception:
                    logger.exception("merger re-render with names failed")
            else:
                final_status = "kevent_partially_completed" if final_status == "kevent_completed" else final_status

        if speaker_tagged is not None:
            updates["speaker_tagged_text"] = speaker_tagged

    # The base text the LLM steps will operate on: the speaker-tagged text
    # if available (richer context for the model), else the raw transcript.
    base_for_llm = speaker_tagged or text

    # Steps 3b-bis → 3f via la sous-fonction composable (refactor v2 §A).
    # Le glossaire effectif fusionne le statique avec les termes du brief
    # auto-lié (si présent), pour que glossary_correction LLM corrige
    # AUSSI les sigles découverts via le brief.
    effective_glossary: list[str] = list(_GLOSSARY_TERMS)
    if brief_glossary_terms_for_llm:
        existing = set(effective_glossary)
        for t in brief_glossary_terms_for_llm:
            if t not in existing:
                effective_glossary.append(t)
                existing.add(t)

    # Charge le glossaire utilisateur global (§5c) pour ce user_sub.
    try:
        if SessionLocal is not None:
            _db_g = SessionLocal()
            try:
                user_sub_g = (payload or {}).get("user_sub") or ""
                if user_sub_g:
                    from app.glossary_loader import load_user_glossary
                    user_terms = load_user_glossary(user_sub_g, _db_g) or set()
                    existing = set(effective_glossary)
                    for t in user_terms:
                        if t and t not in existing:
                            effective_glossary.append(t)
                            existing.add(t)
            finally:
                _db_g.close()
    except Exception:
        logger.exception("kevent: failed to load user glossary for audio=%s", audio_file_id)

    chain_updates, chain_status = _run_llm_chain_for_audio(
        base_for_llm, speaker_tagged,
        llm=llm,
        glossary_terms=effective_glossary,
        include_metadata=True,
    )
    updates.update(chain_updates)
    if chain_status == "kevent_partially_completed":
        final_status = "kevent_partially_completed"

    _set_user_audio_status(audio_file_id, final_status, **updates)
    logger.info(
        "Kevent pipeline finished for %s: status=%s, %d outputs",
        audio_file_id, final_status, sum(1 for k in updates if k != "transcription_engine"),
    )
    # ─── Lot 8 — Hook envoi CR par email aux participants ───────────
    # Best-effort, totalement isolé du pipeline transcription (try/except
    # global). Si la prep liée a ``send_cr_email=True`` et que des
    # participants[].email existent, on appelle l'endpoint
    # ``mesreunions-web /api/meetings/<id>/send-cr`` qui s'occupe du SMTP.
    try:
        _maybe_trigger_cr_email_for_audio(audio_file_id, payload or {})
    except Exception:
        logger.exception("send-cr hook failed for audio=%s (ignored)", audio_file_id)
    # Final notification au PWA mobile (le badge bascule du spinner animé
    # vers l'état terminal — completed / partially_completed).
    external_file_id = (payload or {}).get("file_id") or str(audio_file_id)
    try:
        notify_external_status(
            str(external_file_id),
            final_status,
            f"Pipeline Kevent {final_status}",
        )
    except Exception:
        pass


def _push_to_mcr(audio_file_id, user_sub: str, transcoded_filename: str,
                 file_data, payload: dict) -> None:
    """
    Asynchronously push a file to MCR for transcription. The 4-step sequence
    (refresh exchange → create meeting → presigned URL → PUT binary) is
    classified into 3 error families:

      - MCRAuthError        : refresh expired/revoked. Wipe stored token,
                              mark mcr_auth_failed, NO retry.
      - MCRApplicativeError : 4xx applicative. Mark mcr_rejected, NO retry.
      - MCRTransientError   : 5xx / network. Re-raised so the queue
                              consumer's retry counter handles it.
    """
    client = _build_mcr_client()
    if client is None:
        # Misconfigured but enabled — preserve the file in mcr_push_failed so
        # ops sees something concrete in the dashboard rather than a silent skip.
        _set_user_audio_status(audio_file_id, "mcr_push_failed")
        return

    ciphertext = fetch_ciphertext(user_sub)
    if not ciphertext:
        logger.warning("MCR push: no refresh token stored for user_sub=%s; user must re-login", user_sub)
        _set_user_audio_status(audio_file_id, "mcr_auth_failed")
        return

    try:
        refresh_token = decrypt_secret(ciphertext)
    except Exception:
        logger.exception("MCR push: failed to decrypt refresh token for user_sub=%s", user_sub)
        _set_user_audio_status(audio_file_id, "mcr_auth_failed")
        return

    # Step 1: refresh → access
    try:
        access_token = client.exchange_refresh(refresh_token)
    except MCRAuthError:
        logger.warning("MCR push: refresh rejected by KC for user_sub=%s; deleting stored token", user_sub)
        delete_ciphertext(user_sub)
        _set_user_audio_status(audio_file_id, "mcr_auth_failed")
        return

    # Steps 2-4: create meeting → presigned → PUT
    meeting_payload = {
        "name": payload.get("original_filename", transcoded_filename),
        "name_platform": "IMPORT",
    }
    try:
        meeting_id = client.create_meeting(access_token, meeting_payload)
        presigned = client.generate_presigned(access_token, meeting_id, transcoded_filename)
        # file_data is a BytesIO already loaded in RAM from the audio-internal upload step.
        # Reset position and read raw bytes for the PUT.
        file_data.seek(0)
        body = file_data.read()
        client.upload_binary(presigned, body, _guess_audio_mime(transcoded_filename))
    except MCRAuthError:
        # Token was accepted at exchange but rejected on /meetings — likely stale
        # KC revocation between calls. Treat as auth failure.
        logger.warning("MCR push: meeting/presigned/upload rejected with auth error for %s", audio_file_id)
        delete_ciphertext(user_sub)
        _set_user_audio_status(audio_file_id, "mcr_auth_failed")
        return
    except MCRApplicativeError:
        logger.exception("MCR push: applicative error for %s, marking mcr_rejected", audio_file_id)
        _set_user_audio_status(audio_file_id, "mcr_rejected")
        return
    # MCRTransientError propagates → consumer retries via x-retry-count

    _set_user_audio_status(audio_file_id, "mcr_pushed", mcr_meeting_id=meeting_id)
    logger.info("MCR push success: audio_file_id=%s meeting_id=%s", audio_file_id, meeting_id)


def _perform_pull(payload: dict) -> dict:
    """
    Pull a transcoded file from processed-staging into the internal zone.

    Idempotent: if a UserAudioFile already exists for this internal_key, the
    function returns ``status=already_pulled`` without re-downloading.

    Raises on infrastructure errors (S3 unreachable, DB down) so the caller
    (queue drain or HTTP handler) can decide whether to retry. Returns a
    dict on success suitable for JSON response.
    """
    required = ("file_id", "user_sub", "simple_code", "transcoded_filename")
    missing = [f for f in required if f not in payload]
    if missing:
        raise ValueError(f"Missing fields: {missing}")

    file_id = payload["file_id"]
    user_sub = payload["user_sub"]
    transcoded_filename = payload["transcoded_filename"]
    simple_code = payload["simple_code"]
    auto_transcribe = bool(payload.get("auto_transcribe", True))
    internal_key = f"{user_sub}/{simple_code}/{transcoded_filename}"

    logger.info("Pull request: file_id=%s, user=%s, file=%s", file_id, user_sub, transcoded_filename)

    db = SessionLocal()
    try:
        existing = (
            db.query(UserAudioFile)
            .filter(UserAudioFile.stored_filename == internal_key)
            .first()
        )
    finally:
        db.close()
    if existing:
        logger.info("Idempotent replay detected for %s, key already present: %s", file_id, internal_key)
        notify_external_status(
            file_id,
            "transferred",
            "Fichier déjà intégré (idempotence). Transcription en cours... (100%)",
        )
        return {"status": "already_pulled", "file_id": file_id, "internal_key": internal_key}

    notify_external_status(file_id, "transferring", "Transfert: téléchargement depuis la zone de transit (45%)")
    logger.info("Pulling from processed-staging: %s", transcoded_filename)
    file_data = download_fileobj(s3_processed_cfg, transcoded_filename)
    file_size = file_data.getbuffer().nbytes
    # Capture les bytes AVANT l'upload S3 : boto3.upload_fileobj peut
    # fermer le BytesIO sous le capot (ValueError: I/O operation on closed
    # file vu sur _transcribe_via_kevent.seek(0)). On reconstruit donc un
    # BytesIO frais à partir des bytes en RAM pour le pipeline Kevent.
    audio_bytes_for_transcription = file_data.getvalue()

    notify_external_status(file_id, "transferring", "Transfert: copie vers la zone interne (70%)")
    upload_fileobj(s3_internal_cfg, internal_key, file_data, _guess_audio_mime(transcoded_filename))
    logger.info("Stored internally: %s (%d bytes)", internal_key, file_size)
    # Reconstruit un BytesIO en RAM pour les backends qui réutilisent les
    # bytes (mcr / kevent). Sans ça, file_data peut être fermé après
    # upload et tout le reste plante.
    file_data = BytesIO(audio_bytes_for_transcription)

    notify_external_status(file_id, "transferring", "Transfert: finalisation et indexation (90%)")
    db = SessionLocal()
    try:
        audio_file = UserAudioFile(
            id=uuid4(),
            user_sub=user_sub,
            user_email=payload.get("user_email"),
            original_session_code=simple_code,
            original_filename=payload.get("original_filename", transcoded_filename),
            stored_filename=internal_key,
            file_size_bytes=file_size,
            audio_quality_score=payload.get("quality_score"),
            audio_duration_seconds=payload.get("duration_seconds"),
            transcription_status="pending" if auto_transcribe else "disabled",
        )
        db.add(audio_file)
        db.flush()  # garantit audio_file.id avant la Meeting

        # PR2d : à chaque upload audio, on matérialise une Meeting (CR
        # post-réunion en attente). Lien audio↔meeting dès l'arrivée ;
        # preparation_id reste NULL (l'auto-link plus bas, backend=kevent,
        # s'en charge si une Preparation matche).
        default_title = (
            payload.get("original_filename")
            or transcoded_filename
            or f"Réunion du {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        )
        meeting_row = Meeting(
            id=uuid4(),
            user_sub=user_sub,
            user_audio_file_id=audio_file.id,
            preparation_id=None,
            title=default_title,
            created_at=datetime.now(timezone.utc),
        )
        db.add(meeting_row)
        db.commit()
        audio_file_id = str(audio_file.id)
        logger.info(
            "Created meeting=%s for audio=%s (preparation_id=NULL, auto-link pending)",
            meeting_row.id, audio_file.id,
        )

        # On émet le "transferred 100%" AVANT la dispatch backend pour que
        # les notifications kevent (kevent_queued / kevent_processing /
        # kevent_failed) qui suivent puissent légitimement écraser ce
        # status (le dernier write gagne côté mobile-upload-pwa). Précédemment
        # ce notify était fait après _transcribe_via_kevent et écrasait
        # silencieusement les erreurs d'auth Kevent.
        if auto_transcribe:
            notify_external_status(
                file_id,
                "transferred",
                "Fichier intégré à votre compte. Transcription en cours... (100%)",
            )

        if auto_transcribe:
            # Three mutually-exclusive backends, selected via env. file_data
            # is already in RAM from the audio-internal upload step above —
            # both mcr and kevent reuse it directly without re-downloading.
            backend = (TRANSCRIPTION_BACKEND or "stub").strip().lower()
            if backend == "mcr":
                _push_to_mcr(
                    audio_file_id=audio_file.id,
                    user_sub=user_sub,
                    transcoded_filename=transcoded_filename,
                    file_data=file_data,
                    payload=payload,
                )
            elif backend == "kevent":
                # meeting-prep v2 §B (PR2d) : auto-lien amont audio↔preparation
                # avant transcription. La Meeting créée plus haut dans la même
                # transaction porte le lien : on set ``meeting.preparation_id``
                # si auto-link match. Idempotent : si la meeting porte déjà
                # une preparation (retry queue), on saute. Pas de raise : tout
                # échec downgrade silencieusement en "pas de preparation liée".
                try:
                    meeting_row = (
                        db.query(Meeting)
                        .filter(Meeting.user_audio_file_id == audio_file.id)
                        .first()
                    )
                    if meeting_row is not None and not meeting_row.preparation_id:
                        prep_id, _scored = auto_link_audio_to_preparation(
                            audio_id=audio_file.id,
                            user_sub=user_sub,
                            audio_filename=payload.get("original_filename") or transcoded_filename,
                            audio_upload_at=audio_file.created_at,
                            db=db,
                        )
                        if prep_id:
                            meeting_row.preparation_id = prep_id
                            # Maintient un lien inverse legacy sur UAF pour
                            # consommateurs pas encore migrés (audio_lookup
                            # serializer le tolère aussi).
                            audio_file.meeting_id = meeting_row.id
                            db.commit()
                            logger.info(
                                "auto-link: audio=%s meeting=%s → preparation=%s",
                                audio_file.id, meeting_row.id, prep_id,
                            )
                except Exception:
                    logger.exception(
                        "auto-link: failed for audio=%s (non-fatal, continuing without preparation link)",
                        audio_file.id,
                    )

                _set_user_audio_status(
                    audio_file.id,
                    "kevent_transcribing",
                    transcription_engine="kevent",
                )
                try:
                    _transcribe_via_kevent(
                        audio_file_id=audio_file.id,
                        transcoded_filename=transcoded_filename,
                        file_data=file_data,
                        payload=payload,
                    )
                except KeventTransientError:
                    # Re-raise so the consume_queue retry counter gets to it;
                    # the underlying message stays on internal_pull and will be
                    # re-pushed up to QUEUE_MAX_RETRIES times.
                    logger.exception("Kevent transient error, will retry via queue")
                    raise
            else:  # stub (default)
                try:
                    publish_message(rabbit_cfg, QUEUE_TRANSCRIPTION, {
                        "audio_file_id": audio_file_id,
                        "user_sub": user_sub,
                        "stored_filename": internal_key,
                        "original_filename": payload.get("original_filename"),
                        "simple_code": simple_code,
                    })
                    logger.info("Transcription enqueued for %s", audio_file_id)
                    _set_user_audio_status(audio_file.id, "pending", transcription_engine="stub")
                except Exception as e:
                    logger.warning("Failed to enqueue transcription: %s", e)
        else:
            logger.info("Transcription disabled by token flag for %s", audio_file_id)

    finally:
        db.close()

    # NB: le notify "transferred 100%" cas auto_transcribe est désormais
    # émis AVANT la dispatch backend (cf. plus haut), pour ne pas écraser
    # les status kevent_failed / kevent_completed remontés par
    # _transcribe_via_kevent. On garde ici uniquement la branche
    # "transcription désactivée".
    if not auto_transcribe:
        notify_external_status(
            file_id,
            "transferred",
            "Fichier intégré à votre compte. Transcription automatique désactivée pour ce code. (100%)",
        )

    return {"status": "pulled", "file_id": file_id, "internal_key": internal_key}


def _drain_internal_pull_callback(message: dict) -> bool:
    """Adapter for queue drain: True ⇒ ack, False/exception ⇒ retry counter."""
    try:
        _perform_pull(message)
        return True
    except ValueError:
        # Bad payload — don't retry, just log and drop via the helper's drop path.
        logger.exception("Invalid internal_pull payload, dropping: %s", message)
        return True
    except Exception:
        logger.exception("internal_pull processing failed, will retry via queue counter")
        return False


def _drain_internal_pull_queue() -> int:
    """Drain the internal_pull queue once. Safe to call from anywhere."""
    return drain_queue_once(rabbit_cfg, QUEUE_INTERNAL_PULL, _drain_internal_pull_callback)


def run_internal_purge_once():
    """Purge imported files older than configured age from internal DB/S3."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=INTERNAL_PURGE_MAX_AGE_DAYS)
    db = SessionLocal()
    removed_db = 0
    removed_s3 = 0
    skipped_s3 = 0
    lock_acquired = False
    try:
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": INTERNAL_PURGE_LOCK_ID},
            ).scalar()
        )
        if not lock_acquired:
            logger.debug("Internal purge skipped (lock busy)")
            return

        stale_files = (
            db.query(UserAudioFile)
            .filter(UserAudioFile.created_at < cutoff)
            .all()
        )

        for audio_file in stale_files:
            try:
                delete_object(s3_internal_cfg, audio_file.stored_filename)
                removed_s3 += 1
            except Exception:
                skipped_s3 += 1
                logger.warning("Failed to delete internal object: %s", audio_file.stored_filename)

            db.delete(audio_file)
            removed_db += 1

        # Purge orphan issued_tokens (and their options + non-confirmed devices) :
        #   - expired_unused : expires_at < NOW - 24h grace, never enrolled
        #   - expired_consumed : expires_at < NOW - 90 days, even with history
        # Note : `device_enrollments` survive even when the token is gone (the
        # device_token is signed independently with retention_until). Only purge
        # device rows whose `last_seen_at` is None (= enrollment never confirmed).
        from libs.shared.app.models import IssuedToken, IssuedTokenOption, DeviceEnrollment
        unused_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        consumed_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        # Tokens jamais utilisés (aucun device confirmé) — purge à 24h après expiration
        unused_tokens = (
            db.query(IssuedToken)
            .outerjoin(DeviceEnrollment, DeviceEnrollment.simple_code == IssuedToken.simple_code)
            .filter(IssuedToken.expires_at < unused_cutoff)
            .filter(DeviceEnrollment.id.is_(None))
            .all()
        )
        # Tokens consumed (avec device) — purge à 90j après expiration. Plus
        # rare ; le device_token signé reste valide jusqu'à sa retention_until.
        consumed_tokens = (
            db.query(IssuedToken)
            .filter(IssuedToken.expires_at < consumed_cutoff)
            .all()
        )
        purged_tokens = 0
        for tok in {t.id: t for t in (unused_tokens + consumed_tokens)}.values():
            db.query(IssuedTokenOption).filter(
                IssuedTokenOption.simple_code == tok.simple_code
            ).delete(synchronize_session=False)
            db.query(DeviceEnrollment).filter(
                DeviceEnrollment.simple_code == tok.simple_code,
                DeviceEnrollment.last_seen_at.is_(None),  # only orphan/never-confirmed
            ).delete(synchronize_session=False)
            db.delete(tok)
            purged_tokens += 1

        db.commit()
        if removed_db or purged_tokens:
            logger.info(
                "Internal purge done: audio_files=%d (s3_deleted=%d, s3_failed=%d), "
                "issued_tokens=%d, cutoff=%s",
                removed_db, removed_s3, skipped_s3, purged_tokens, cutoff.isoformat()
            )
    except Exception:
        db.rollback()
        logger.exception("Internal purge failed")
    finally:
        if lock_acquired:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": INTERNAL_PURGE_LOCK_ID},
                )
                db.commit()
            except Exception:
                db.rollback()
                logger.warning("Failed to release internal purge lock")
        db.close()


def _purge_loop():
    logger.info(
        "Starting internal purge loop: interval=%ss, max_age=%sd",
        INTERNAL_PURGE_INTERVAL_SECONDS,
        INTERNAL_PURGE_MAX_AGE_DAYS,
    )
    while True:
        run_internal_purge_once()
        time.sleep(INTERNAL_PURGE_INTERVAL_SECONDS)


def _pull_queue_loop():
    """Poll-drain internal_pull at the configured cadence."""
    logger.info(
        "Starting internal_pull drain loop: interval=%ss",
        INTERNAL_PULL_QUEUE_INTERVAL_SECONDS,
    )
    while True:
        try:
            handled = _drain_internal_pull_queue()
            if handled:
                logger.info("Drained %d message(s) from internal_pull", handled)
        except Exception:
            logger.exception("Drain loop iteration failed; will retry next tick")
        time.sleep(INTERNAL_PULL_QUEUE_INTERVAL_SECONDS)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "zone": "internal"})


@app.route("/healthz")
def healthz():
    return health()


@app.route("/api/v1/pull", methods=["POST"])
def pull_file():
    """
    Legacy endpoint preserved for backward compatibility with single-cluster
    deployments where in-cluster DNS resolves between zones (e.g.
    docker-compose, integration). In prod-bêta this route is unreachable
    from the DMZ; the trigger goes through /api/v1/pull-trigger instead.
    """
    if not verify_token():
        logger.warning("Unauthorized pull request from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    try:
        result = _perform_pull(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("Failed to pull file %s", data.get("file_id"))
        return jsonify({"error": "Internal error during pull"}), 500
    return jsonify(result)


@app.route("/api/v1/queue-status", methods=["GET"])
def queue_status():
    """File d'attente Kevent — proxy lite vers gateway list_jobs.

    Permet à mobile-upload-pwa / mesreunions-web de surfacer une info brève
    "Position X/Y dans la file" sans exposer la clé API.

    Query: ``service_type`` (défaut audio), ``job_id`` (optional → calcule
    position+ETA pour ce job spécifique).
    Auth: INTERNAL_API_TOKEN bearer (cf autres endpoints internes).

    Cache mémoire TTL 5s par (service_type) car notre clé API = unique
    consumer → la response est la même pour tous les callers.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    service_type = (request.args.get("service_type") or "audio").strip()
    job_id = (request.args.get("job_id") or "").strip() or None

    payload = _queue_status_cache_get_or_fetch(service_type)
    if payload is None:
        # Gateway indisponible → réponse neutre (la UI gérera "indisponible").
        from datetime import datetime, timezone as _tz
        return jsonify({
            "pending_total": None,
            "processing_total": None,
            "your_position": None,
            "eta_seconds": None,
            "throughput_per_min": None,
            "stale": True,
            "fetched_at": datetime.now(_tz.utc).isoformat(),
        }), 503
    from libs.shared.app.queue_eta import compute_queue_summary
    import dataclasses
    summary = compute_queue_summary(payload, own_job_id=job_id)
    return jsonify(dataclasses.asdict(summary))


@app.route("/api/v1/audio/meeting-datetimes", methods=["GET"])
def audio_meeting_datetimes():
    """Bulk map (simple_code, original_filename) → meeting_datetime override.

    Renvoie uniquement les rows ayant un override non-NULL — sert au
    mesreunions-web pour enrichir la liste mydevices d'un seul aller-retour
    (au lieu de N appels lookup individuels). Format compact :
    ``{"items": [{"simple_code","original_filename","meeting_datetime"}]}``.
    Auth = INTERNAL_API_TOKEN bearer.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    user_sub = (request.args.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400
    if SessionLocal is None:
        return jsonify({"error": "db_not_ready"}), 503
    db = SessionLocal()
    try:
        rows = (
            db.query(
                UserAudioFile.original_session_code,
                UserAudioFile.original_filename,
                UserAudioFile.meeting_datetime,
            )
            .filter(
                UserAudioFile.user_sub == user_sub,
                UserAudioFile.meeting_datetime.isnot(None),
            )
            .all()
        )
        items = [
            {
                "simple_code": code,
                "original_filename": name,
                "meeting_datetime": dt.isoformat() if dt else None,
            }
            for (code, name, dt) in rows
        ]
        return jsonify({"items": items})
    except Exception:
        logger.exception("audio_meeting_datetimes failed")
        return jsonify({"error": "internal_error"}), 500
    finally:
        db.close()


@app.route("/api/v1/audio/lookup", methods=["POST"])
def audio_lookup():
    """Return all transcription/diarization outputs for a user audio file.

    Identified by ``(user_sub, original_session_code, stored_filename)``.
    Used by mesreunions-web to back the user-facing download endpoints
    (transcript .txt/.md/.docx/.odt, meeting-cr .json/.md/.docx/.odt).
    Auth = INTERNAL_API_TOKEN bearer (same as ``/api/v1/pull``) — only
    callable from the internal zone.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    simple_code = (data.get("simple_code") or "").strip()
    filename = (data.get("stored_filename") or "").strip()
    if not user_sub or not simple_code or not filename:
        return jsonify({"error": "user_sub, simple_code, stored_filename required"}), 400
    if SessionLocal is None:
        return jsonify({"error": "db_not_ready"}), 503
    db = SessionLocal()
    try:
        # ``filename`` côté mesreunions-web c'est uploaded_files.transcoded_filename
        # (basename, ex: YJENNB_xxx_foo.mp4). Côté user_audio_files,
        # stored_filename est la clé S3 interne complète préfixée par
        # ``<user_sub>/<simple_code>/`` (cf. _perform_pull). On matche
        # donc en suffixe pour rester compatible avec les deux formats
        # (lookup historique avec basename, et nouveaux uploads).
        from sqlalchemy import or_
        row = (
            db.query(UserAudioFile)
            .filter(
                UserAudioFile.user_sub == user_sub,
                UserAudioFile.original_session_code == simple_code,
                or_(
                    UserAudioFile.stored_filename == filename,
                    UserAudioFile.stored_filename.like(f"%/{filename}"),
                ),
            )
            .first()
        )
        if row is None:
            return jsonify({"error": "not_found"}), 404
        # PR2d : résout meeting + preparation liés (via meetings.user_audio_file_id
        # ou ancien lien direct user_audio_files.meeting_id si encore set).
        meeting_id = None
        preparation_id = None
        try:
            m = (
                db.query(Meeting)
                .filter(
                    Meeting.user_sub == user_sub,
                    Meeting.user_audio_file_id == row.id,
                    Meeting.trashed_at.is_(None),
                )
                .first()
            )
            if m is not None:
                meeting_id = str(m.id)
                preparation_id = str(m.preparation_id) if m.preparation_id else None
            elif getattr(row, "meeting_id", None):
                # Cas legacy : UAF.meeting_id pointe encore vers une row meetings
                # sans réciproque (UAF.meeting_id écrit avant que meeting.user_audio_file_id
                # ne devienne la source de vérité).
                meeting_id = str(row.meeting_id)
        except Exception:
            logger.exception("audio_lookup: failed to resolve meeting/preparation for %s", row.id)
        return jsonify({
            "id": str(row.id),
            "transcription_status": row.transcription_status,
            "transcription_engine": row.transcription_engine,
            "transcription_language": row.transcription_language,
            "transcription_text": row.transcription_text,
            "speaker_tagged_text": row.speaker_tagged_text,
            "glossary_corrected_text": row.glossary_corrected_text,
            "cleaned_text": row.cleaned_text,
            "reformulated_text": row.reformulated_text,
            "meeting_analysis_json": row.meeting_analysis_json,
            "diarization_json": row.diarization_json,
            "audio_quality_score": row.audio_quality_score,
            "audio_duration_seconds": row.audio_duration_seconds,
            "transcription_completed_at": (
                row.transcription_completed_at.isoformat()
                if row.transcription_completed_at else None
            ),
            "suggested_filename": row.suggested_filename,
            "key_points_summary": row.key_points_summary,
            "kevent_job_id": row.kevent_job_id,
            "meeting_datetime": (
                row.meeting_datetime.isoformat() if row.meeting_datetime else None
            ),
            # PR2d : liens canoniques meeting/preparation. L'alias legacy
            # ``meeting_brief_id`` est gardé en double pour migration progressive
            # des consommateurs (à retirer après PR3/PR4).
            "meeting_id": meeting_id,
            "preparation_id": preparation_id,
            "meeting_brief_id": preparation_id,  # alias legacy
        })
    except Exception:
        logger.exception("audio_lookup failed")
        return jsonify({"error": "internal_error"}), 500
    finally:
        db.close()


@app.route("/api/v1/pull-trigger", methods=["POST"])
def pull_trigger():
    """
    Optional cross-cluster wake-up. Bearer-protected with
    ``INTERNAL_PUSH_TRIGGER_TOKEN`` (distinct from INTERNAL_API_TOKEN so the
    two can be rotated independently). The body is ignored — invocation is
    a pure "drain now" signal. The actual messages live on the
    ``internal_pull`` AMQP queue and are the source of truth.
    """
    if not verify_trigger_token():
        logger.warning("Unauthorized trigger request from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401
    if not _client_ip_allowed(request.remote_addr or ""):
        logger.warning("Trigger IP not in allowlist: %s", request.remote_addr)
        return jsonify({"error": "Forbidden"}), 403
    try:
        handled = _drain_internal_pull_queue()
    except Exception:
        logger.exception("Trigger-driven drain failed")
        return jsonify({"error": "drain_failed"}), 500
    return jsonify({"status": "ok", "drained": handled})


# ─── Meeting-prep v2 : auto-link audio↔brief (cf §4 du plan) ────


def _tokenize_fr(s: str) -> set:
    """Tokenisation simple FR pour le scoring Jaccard."""
    import re as _re
    if not s:
        return set()
    return {t for t in _re.findall(r"[a-z0-9éèêàâïôûç]{3,}", s.lower())}


def _score_brief_for_audio(brief, audio_filename: str,
                            audio_upload_at: datetime,
                            already_linked_briefs: set) -> tuple:
    """Calcule le score multi-signaux d'une Preparation candidate pour un audio.

    Retourne ``(score, breakdown)``. Cf §4 du plan : poids
    similarity=0.30, temporal=0.40, drive_colocation=0.10,
    engagement=0.10, anti_rebound=0.10.

    Le nom de la fonction conserve "brief" pour ne pas casser les tests
    unitaires existants (`tests/unit/test_auto_link_audio_to_brief.py`) ;
    sémantiquement on score une Preparation. Le helper lit ``content`` (nom
    canonique migration 012) avec fallback sur ``brief_json`` pour rester
    compatible avec les SimpleNamespace de tests legacy.
    """
    bsubj = (getattr(brief, "subject", None) or getattr(brief, "title", None) or "") + " "
    bj = getattr(brief, "content", None)
    if bj is None:
        bj = getattr(brief, "brief_json", None) or {}
    if isinstance(bj, dict):
        bsubj += (bj.get("objective_reformulated") or "")
    a_tokens = _tokenize_fr(audio_filename)
    b_tokens = _tokenize_fr(bsubj)
    union = a_tokens | b_tokens
    sim = (len(a_tokens & b_tokens) / len(union)) if union else 0.0

    # Proximité temporelle.
    prox = 0.0
    bc = getattr(brief, "created_at", None)
    if bc and audio_upload_at:
        delta_hours = abs((audio_upload_at - bc).total_seconds()) / 3600.0
        if delta_hours <= 24:
            prox = 1.0
        elif delta_hours >= 24 * 14:
            prox = 0.0
        else:
            prox = 1.0 - (delta_hours - 24) / (24 * 14 - 24)

    # Co-localisation Drive (préfixe commun ≥ 4 chars).
    drive_loc = 0.0
    folder = getattr(brief, "drive_folder_id", None) or ""
    if folder and audio_filename:
        common = 0
        for ca, cb in zip(audio_filename.lower(), folder.lower()):
            if ca == cb:
                common += 1
            else:
                break
        if common >= 4:
            drive_loc = 1.0

    # Engagement récent.
    engagement = 0.0
    lv = getattr(brief, "last_viewed_at", None)
    if lv:
        delta = datetime.now(timezone.utc) - lv
        if delta.total_seconds() <= 24 * 3600:
            engagement = 1.0

    # Anti-rebond.
    anti_rebound = 0.0 if str(brief.id) in already_linked_briefs else 1.0

    score = 0.30 * sim + 0.40 * prox + 0.10 * drive_loc + 0.10 * engagement + 0.10 * anti_rebound
    return score, {
        "similarity": round(sim, 3),
        "temporal": round(prox, 3),
        "drive_colocation": round(drive_loc, 3),
        "engagement": round(engagement, 3),
        "anti_rebound": round(anti_rebound, 3),
    }


def auto_link_audio_to_preparation(audio_id, user_sub: str,
                                    audio_filename: str,
                                    audio_upload_at: datetime,
                                    *, db=None,
                                    score_threshold: float = 0.55,
                                    top2_gap: float = 0.15):
    """Auto-lien déterministe avant transcription (§4 du plan, PR2d).

    Cherche une Preparation matching pour le user_sub sur les 30 derniers
    jours. Match strict : top-1 > ``score_threshold`` ET écart ≥ ``top2_gap``
    avec le top-2 (sinon ambiguïté). Pas de DB write côté audio/meeting ici :
    le caller (`_perform_pull`) s'occupe de poser ``meeting.preparation_id``
    sur la Meeting créée à l'upload.

    Anti-rebond : exclut les preparations déjà référencées par une autre
    Meeting (`meetings.preparation_id IS NOT NULL`) du même user.

    Retourne ``(preparation_id|None, scored_candidates)``. Chaque entry de
    `scored_candidates` porte la clé canonique ``preparation_id`` ainsi que
    l'alias legacy ``brief_id`` (= même valeur) pour rétrocompat tests.
    ``db`` peut être fourni pour tests ; sinon ouvre une SessionLocal locale.
    """
    owns_db = db is None
    if owns_db:
        if SessionLocal is None:
            return None, []
        db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        candidates = (
            db.query(Preparation)
            .filter(
                Preparation.user_sub == user_sub,
                Preparation.trashed_at.is_(None),
                Preparation.created_at > cutoff,
            )
            .all()
        )
        if not candidates:
            return None, []

        # Anti-rebond : preparations déjà liées via une Meeting du même user.
        linked_prep_ids = {
            str(m.preparation_id)
            for m in db.query(Meeting)
            .filter(Meeting.user_sub == user_sub,
                    Meeting.preparation_id.isnot(None),
                    Meeting.trashed_at.is_(None))
            .all()
        }

        scored = []
        for b in candidates:
            score, breakdown = _score_brief_for_audio(
                b, audio_filename, audio_upload_at, linked_prep_ids,
            )
            scored.append({
                "preparation_id": str(b.id),
                "brief_id": str(b.id),  # alias legacy (tests + consommateurs)
                "score": score,
                "breakdown": breakdown,
            })
        scored.sort(key=lambda x: x["score"], reverse=True)

        if not scored:
            return None, []
        top1 = scored[0]
        if top1["score"] <= score_threshold:
            return None, scored
        if len(scored) >= 2:
            gap = top1["score"] - scored[1]["score"]
            if gap < top2_gap:
                return None, scored
        return top1["preparation_id"], scored
    finally:
        if owns_db:
            db.close()


# Alias rétrocompat : conserve l'ancien nom de fonction pour les tests
# unitaires existants et les éventuels imports tiers. Sémantiquement
# identique — retourne une preparation_id.
auto_link_audio_to_brief = auto_link_audio_to_preparation


# ─── Re-traitement avec glossaire amendé (§5 du plan) ────────────


@app.route("/api/v1/audio/<audio_id>/reprocess", methods=["POST"])
def reprocess_audio(audio_id: str):
    """Relance la chaîne LLM (glossary_correction → ... → meeting_analysis)
    avec un glossaire fusionné (statique + termes du brief + glossaire user).

    Body: ``{user_sub, glossary_from_brief_id?, force?}``.

    Idempotence : si ``reprocessed_with_meeting_id == glossary_from_brief_id``
    et ``!force`` → 200 avec ``{reprocessed: false, reason: 'idempotent'}``.
    Pose un advisory lock postgres sur audio_id pour éviter concurrence.

    .. note::

       **Implémentation partielle (meeting-prep v2).** Le squelette du flux
       (idempotence, lock, lecture du brief, fusion glossaire) est en place,
       mais la séquence LLM (glossary_correction → oob_cleaning →
       reformulation → meeting_analysis → absentee_summary) n'est PAS
       encore branchée — elle dépend d'une factorisation de
       ``_transcribe_via_kevent`` en sous-fonctions composables (cf TODO
       l.415 du fichier). Pour l'instant, l'endpoint répond 501 quand un
       run effectif est demandé, sauf pour le cas idempotent.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    brief_id = data.get("glossary_from_brief_id") or None
    force = bool(data.get("force"))
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503

    from libs.shared.app.models import UserAudioFile
    db = SessionLocal()
    try:
        uaf = (
            db.query(UserAudioFile)
            .filter(UserAudioFile.id == audio_id,
                    UserAudioFile.user_sub == user_sub)
            .first()
        )
        if not uaf:
            return jsonify({"error": "not_found"}), 404
        if not uaf.transcription_text and not uaf.speaker_tagged_text:
            return jsonify({"error": "no_transcription"}), 410

        # Idempotence (lit la colonne canonique migration 012).
        prev = str(uaf.reprocessed_with_meeting_id) if uaf.reprocessed_with_meeting_id else None
        if not force and prev == (brief_id or None):
            return jsonify({"reprocessed": False, "reason": "idempotent"}), 200

        # Advisory lock postgres (best-effort — pas en SQLite).
        try:
            db.execute(
                text("SELECT pg_try_advisory_xact_lock(:k1, :k2)"),
                {"k1": 4242, "k2": hash(str(audio_id)) & 0x7FFFFFFF},
            )
        except Exception:
            pass  # SQLite ou pg_locks indisponible.

        # Snapshot état actuel → reprocess_history (cap 5 FIFO).
        prev_entry = {
            "version": int(uaf.reprocess_version or 0),
            "at": datetime.now(timezone.utc).isoformat(),
            "brief_id": prev,
            "glossary_term_count": None,  # rempli plus bas
            "prev": {
                "glossary_corrected_text": uaf.glossary_corrected_text,
                "reformulated_text": uaf.reformulated_text,
                "key_points_summary": uaf.key_points_summary,
                "meeting_analysis_json": uaf.meeting_analysis_json,
            },
        }
        history = list(uaf.reprocess_history or [])

        # Construit le glossaire fusionné : statique + brief + user.
        effective_glossary: list[str] = list(_GLOSSARY_TERMS)
        seen = set(effective_glossary)
        brief_terms_added = 0
        if brief_id:
            # `brief_id` ici vient du body `glossary_from_brief_id` côté API ;
            # sémantiquement c'est une preparation_id (migration 012). Le
            # paramètre garde son nom legacy pour ne pas casser les clients.
            brief_terms, _ = _fetch_preparation_glossary_terms(brief_id, db)
            for t in brief_terms:
                if t and t not in seen:
                    effective_glossary.append(t)
                    seen.add(t)
                    brief_terms_added += 1
        try:
            from app.glossary_loader import load_user_glossary
            for t in load_user_glossary(user_sub, db) or set():
                if t and t not in seen:
                    effective_glossary.append(t)
                    seen.add(t)
        except Exception:
            logger.exception("reprocess: failed to load user glossary for %s", user_sub)
        prev_entry["glossary_term_count"] = len(effective_glossary)

        # Pose status intermédiaire + bump version.
        new_version = int(uaf.reprocess_version or 0) + 1
        history.append(prev_entry)
        if len(history) > 5:
            history = history[-5:]
        try:
            uaf.transcription_status = "kevent_reprocessing"
            uaf.reprocess_version = new_version
            uaf.reprocess_history = history
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("reprocess: failed to commit reprocessing status for %s", audio_id)
            return jsonify({"error": "db_write_failed"}), 500

        # Relance la chaîne LLM via la sous-fonction composable.
        # SKIP transcription / diarisation / speaker_naming.
        base_for_llm = uaf.speaker_tagged_text or uaf.glossary_corrected_text or uaf.transcription_text
        speaker_tagged = uaf.speaker_tagged_text
        llm = _build_llm_client()
        chain_updates, chain_status = _run_llm_chain_for_audio(
            base_for_llm, speaker_tagged,
            llm=llm,
            glossary_terms=effective_glossary,
            include_metadata=False,  # garde le filename/key_points existants
        )

        # Status final + tracking reprocess.
        final_status = (
            "kevent_completed"
            if chain_status == "kevent_completed"
            else "kevent_partially_completed"
        )
        chain_updates["reprocessed_with_meeting_id"] = brief_id
        chain_updates["last_reprocessed_at"] = datetime.now(timezone.utc)
        _set_user_audio_status(audio_id, final_status, **chain_updates)

        logger.info(
            "reprocess: audio=%s done version=%d brief_id=%s glossary_terms=%d outputs=%d",
            audio_id, new_version, brief_id, len(effective_glossary), len(chain_updates),
        )
        return jsonify({
            "reprocessed": True,
            "glossary_terms_used": len(effective_glossary),
            "brief_terms_added": brief_terms_added,
            "version": new_version,
            "status": final_status,
        }), 200
    finally:
        db.close()


# ─── User feedback (migration 015) ─────────────────────────────────
#
# 4 endpoints CRUD pour la table user_feedback :
#   POST   /api/v1/feedback                          → insert
#   GET    /api/v1/feedback/mine?user_sub=…          → list propre à un user
#   GET    /api/v1/feedback/all?status=…             → list admin (tout user)
#   PATCH  /api/v1/feedback/<id>                     → update (status, comment)
# Tous Bearer-protégés par INTERNAL_API_TOKEN — mesreunions-web ajoute
# côté wrapper une couche d'autorisation utilisateur (admin claim
# requis pour /all et PATCH).


def _feedback_to_dict(fb):
    """Sérialise une row UserFeedback en dict JSON-safe."""
    return {
        "id": str(fb.id),
        "user_sub": fb.user_sub,
        "file_id": str(fb.file_id) if fb.file_id else None,
        "type": fb.type,
        "payload": fb.payload or {},
        "status": fb.status,
        "ai_suggestion": fb.ai_suggestion,
        "processed_at": fb.processed_at.isoformat() if fb.processed_at else None,
        "processed_by": fb.processed_by,
        "admin_comment": fb.admin_comment,
        "created_at": fb.created_at.isoformat() if fb.created_at else None,
    }


@app.route("/api/v1/feedback", methods=["POST"])
def create_feedback():
    """Body : {user_sub, type, file_id?, payload}.

    type ∈ {'usefulness', 'regenerate'}. file_id facultatif (peut être
    un feedback global). payload : dict libre (schéma documenté dans
    la migration 015).
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    fb_type = (data.get("type") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400
    if fb_type not in ("usefulness", "regenerate"):
        return jsonify({"error": "type must be 'usefulness' or 'regenerate'"}), 400
    file_id = data.get("file_id")
    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "payload must be a JSON object"}), 400

    from libs.shared.app.models import UserFeedback
    db = SessionLocal()
    try:
        fb = UserFeedback(
            user_sub=user_sub,
            file_id=file_id if file_id else None,
            type=fb_type,
            payload=payload,
            status="new",
        )
        db.add(fb)
        db.commit()
        db.refresh(fb)
        logger.info("Feedback created id=%s type=%s user_sub=%s file_id=%s",
                    fb.id, fb_type, user_sub, file_id)
        return jsonify(_feedback_to_dict(fb)), 201
    except Exception:
        db.rollback()
        logger.exception("Feedback create failed")
        return jsonify({"error": "internal"}), 500
    finally:
        db.close()


@app.route("/api/v1/feedback/mine", methods=["GET"])
def list_feedback_mine():
    """Query : ?user_sub=<sub>&limit=50&offset=0.

    Retourne `{items: [...], total: N}` pour la vue "Mes feedbacks"
    côté useful-data tab.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503
    user_sub = (request.args.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400
    limit = max(1, min(int(request.args.get("limit", 50)), 200))
    offset = max(0, int(request.args.get("offset", 0)))

    from libs.shared.app.models import UserFeedback
    db = SessionLocal()
    try:
        q = db.query(UserFeedback).filter(UserFeedback.user_sub == user_sub)
        total = q.count()
        items = q.order_by(UserFeedback.created_at.desc()).offset(offset).limit(limit).all()
        return jsonify({
            "items": [_feedback_to_dict(fb) for fb in items],
            "total": total, "limit": limit, "offset": offset,
        })
    finally:
        db.close()


@app.route("/api/v1/feedback/all", methods=["GET"])
def list_feedback_all():
    """Query : ?status=<new|processed|dismissed>&limit=&offset=.

    Retourne tous les feedbacks (vue admin). Filtre status optionnel.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503
    status_filter = (request.args.get("status") or "").strip()
    limit = max(1, min(int(request.args.get("limit", 100)), 500))
    offset = max(0, int(request.args.get("offset", 0)))

    from libs.shared.app.models import UserFeedback
    db = SessionLocal()
    try:
        q = db.query(UserFeedback)
        if status_filter:
            q = q.filter(UserFeedback.status == status_filter)
        total = q.count()
        items = q.order_by(UserFeedback.created_at.desc()).offset(offset).limit(limit).all()
        return jsonify({
            "items": [_feedback_to_dict(fb) for fb in items],
            "total": total, "limit": limit, "offset": offset,
        })
    finally:
        db.close()


@app.route("/api/v1/feedback/<feedback_id>", methods=["PATCH"])
def update_feedback(feedback_id: str):
    """Body : {status?, admin_comment?, ai_suggestion?, processed_by?}.

    Met à jour les marqueurs admin. Si status='processed', renseigne
    processed_at = now() automatiquement.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503
    data = request.get_json(silent=True) or {}

    from libs.shared.app.models import UserFeedback
    db = SessionLocal()
    try:
        fb = db.query(UserFeedback).filter(UserFeedback.id == feedback_id).first()
        if not fb:
            return jsonify({"error": "not_found"}), 404
        if "status" in data:
            new_status = (data.get("status") or "").strip()
            if new_status not in ("new", "processed", "dismissed"):
                return jsonify({"error": "invalid status"}), 400
            fb.status = new_status
            if new_status == "processed" and not fb.processed_at:
                fb.processed_at = datetime.now(timezone.utc)
        if "admin_comment" in data:
            fb.admin_comment = data.get("admin_comment") or None
        if "ai_suggestion" in data:
            fb.ai_suggestion = data.get("ai_suggestion") or None
        if "processed_by" in data:
            fb.processed_by = data.get("processed_by") or None
        db.commit()
        db.refresh(fb)
        return jsonify(_feedback_to_dict(fb))
    except Exception:
        db.rollback()
        logger.exception("Feedback update failed")
        return jsonify({"error": "internal"}), 500
    finally:
        db.close()


# ─── User glossary CRUD (édition manuelle) ────────────────────────
#
# Permet à l'utilisateur d'éditer son glossaire personnel sans passer
# par une préparation de réunion : ajouter un sigle, blacklister un
# faux terme, marquer un terme comme "curated" (priorité whisper
# initial_prompt). La table user_glossary_terms existe depuis
# migration 011 mais n'était utilisée qu'en lecture jusqu'ici.
#
# Endpoints (tous Bearer INTERNAL_API_TOKEN) :
#   GET    /api/v1/user-glossary?user_sub=…&limit=&offset=
#   POST   /api/v1/user-glossary  body {user_sub, term}  (add manuel curated)
#   PATCH  /api/v1/user-glossary  body {user_sub, term, curated_by_user?, blacklisted?}
#   DELETE /api/v1/user-glossary  body {user_sub, term}


def _glossary_to_dict(g):
    return {
        "user_sub": g.user_sub,
        "term": g.term,
        "first_seen_at": g.first_seen_at.isoformat() if g.first_seen_at else None,
        "last_seen_at": g.last_seen_at.isoformat() if g.last_seen_at else None,
        "occurrence_count": int(g.occurrence_count or 0),
        "last_source_meeting_id": str(g.last_source_meeting_id) if g.last_source_meeting_id else None,
        "curated_by_user": bool(g.curated_by_user),
        "blacklisted": bool(g.blacklisted),
    }


@app.route("/api/v1/user-glossary", methods=["GET"])
def list_user_glossary():
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503
    user_sub = (request.args.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400
    limit = max(1, min(int(request.args.get("limit", 500)), 1000))
    offset = max(0, int(request.args.get("offset", 0)))

    from libs.shared.app.models import UserGlossaryTerm
    db = SessionLocal()
    try:
        q = db.query(UserGlossaryTerm).filter(UserGlossaryTerm.user_sub == user_sub)
        total = q.count()
        items = q.order_by(
            UserGlossaryTerm.curated_by_user.desc(),
            UserGlossaryTerm.occurrence_count.desc(),
            UserGlossaryTerm.term,
        ).offset(offset).limit(limit).all()
        return jsonify({
            "items": [_glossary_to_dict(g) for g in items],
            "total": total, "limit": limit, "offset": offset,
        })
    finally:
        db.close()


@app.route("/api/v1/user-glossary", methods=["POST"])
def add_user_glossary_term():
    """Body : {user_sub, term}. Ajoute un terme curaté (priorité whisper)."""
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    term = (data.get("term") or "").strip()
    if not user_sub or not term:
        return jsonify({"error": "user_sub and term required"}), 400
    if len(term) > 255:
        return jsonify({"error": "term too long (max 255)"}), 400

    from libs.shared.app.models import UserGlossaryTerm
    db = SessionLocal()
    try:
        existing = db.query(UserGlossaryTerm).filter_by(
            user_sub=user_sub, term=term,
        ).first()
        if existing:
            # Idempotent : si déjà présent on le marque juste curated + déblackliste.
            existing.curated_by_user = True
            existing.blacklisted = False
            existing.last_seen_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(existing)
            return jsonify(_glossary_to_dict(existing)), 200
        g = UserGlossaryTerm(
            user_sub=user_sub, term=term,
            first_seen_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
            occurrence_count=1,
            curated_by_user=True,
            blacklisted=False,
        )
        db.add(g)
        db.commit()
        db.refresh(g)
        return jsonify(_glossary_to_dict(g)), 201
    except Exception:
        db.rollback()
        logger.exception("user-glossary add failed")
        return jsonify({"error": "internal"}), 500
    finally:
        db.close()


@app.route("/api/v1/user-glossary", methods=["PATCH"])
def patch_user_glossary_term():
    """Body : {user_sub, term, curated_by_user?, blacklisted?}.

    Toggle d'un état sans recréer la row. Si term inexistant → 404.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    term = (data.get("term") or "").strip()
    if not user_sub or not term:
        return jsonify({"error": "user_sub and term required"}), 400

    from libs.shared.app.models import UserGlossaryTerm
    db = SessionLocal()
    try:
        g = db.query(UserGlossaryTerm).filter_by(user_sub=user_sub, term=term).first()
        if not g:
            return jsonify({"error": "not_found"}), 404
        if "curated_by_user" in data:
            g.curated_by_user = bool(data.get("curated_by_user"))
        if "blacklisted" in data:
            g.blacklisted = bool(data.get("blacklisted"))
        db.commit()
        db.refresh(g)
        return jsonify(_glossary_to_dict(g))
    except Exception:
        db.rollback()
        return jsonify({"error": "internal"}), 500
    finally:
        db.close()


@app.route("/api/v1/user-glossary", methods=["DELETE"])
def delete_user_glossary_term():
    """Body : {user_sub, term}. Suppression définitive."""
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    term = (data.get("term") or "").strip()
    if not user_sub or not term:
        return jsonify({"error": "user_sub and term required"}), 400

    from libs.shared.app.models import UserGlossaryTerm
    db = SessionLocal()
    try:
        n = db.query(UserGlossaryTerm).filter_by(user_sub=user_sub, term=term).delete()
        db.commit()
        if n == 0:
            return jsonify({"error": "not_found"}), 404
        return jsonify({"deleted": 1})
    except Exception:
        db.rollback()
        return jsonify({"error": "internal"}), 500
    finally:
        db.close()


# ─── Correction inline d'un terme sur une transcription existante ─────
#
# 3-en-1 : ajout au glossaire / patch texte / reprocess LLM. Le caller
# choisit quelles actions appliquer via flags booléens.


@app.route("/api/v1/audio/<audio_id>/correct-term", methods=["POST"])
def correct_audio_term(audio_id: str):
    """Body : {user_sub, old, new, add_to_glossary?, patch_text?, reprocess_llm?}.

    Trace systématiquement la correction dans user_feedback
    (type='correction') quels que soient les flags.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    old = (data.get("old") or "").strip()
    new = (data.get("new") or "").strip()
    add_to_glossary = bool(data.get("add_to_glossary"))
    patch_text = bool(data.get("patch_text"))
    reprocess_llm = bool(data.get("reprocess_llm"))
    if not user_sub or not old or not new:
        return jsonify({"error": "user_sub, old, new required"}), 400

    from libs.shared.app.models import UserAudioFile, UserGlossaryTerm, UserFeedback
    db = SessionLocal()
    applied = []
    try:
        uaf = db.query(UserAudioFile).filter(
            UserAudioFile.id == audio_id,
            UserAudioFile.user_sub == user_sub,
        ).first()
        if not uaf:
            return jsonify({"error": "not_found"}), 404

        # 1) Glossaire — ajoute/curated le terme cible (new).
        if add_to_glossary:
            g = db.query(UserGlossaryTerm).filter_by(user_sub=user_sub, term=new).first()
            if g:
                g.curated_by_user = True
                g.blacklisted = False
                g.last_seen_at = datetime.now(timezone.utc)
            else:
                g = UserGlossaryTerm(
                    user_sub=user_sub, term=new,
                    first_seen_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                    occurrence_count=1,
                    curated_by_user=True, blacklisted=False,
                )
                db.add(g)
            applied.append("glossary")

        # 2) Patch text — remplace `old` par `new` dans les 4 colonnes
        # de texte (transcription / speaker-tagged / cleaned / reformulated).
        # str.replace est case-sensitive → OK : on garde la forme exacte
        # de l'utilisateur. Limite implicite : pas de regex (= match exact).
        if patch_text and old:
            cols = (
                "transcription_text", "speaker_tagged_text",
                "glossary_corrected_text", "cleaned_text", "reformulated_text",
            )
            for c in cols:
                v = getattr(uaf, c, None)
                if isinstance(v, str) and old in v:
                    setattr(uaf, c, v.replace(old, new))
            applied.append("patch_text")

        db.commit()

        # 3) Reprocess LLM — délègue à l'endpoint existant.
        reprocess_status = None
        if reprocess_llm:
            try:
                # Appel HTTP local (l'endpoint est dans le même process).
                import urllib.request as _ur, json as _json
                req2 = _ur.Request(
                    f"http://localhost:{int(os.getenv('FILE_PULLER_PORT', 8090))}/api/v1/audio/{audio_id}/reprocess",
                    data=_json.dumps({"user_sub": user_sub, "force": True}).encode(),
                    headers={"Authorization": request.headers.get("Authorization", ""),
                             "Content-Type": "application/json"},
                    method="POST",
                )
                with _ur.urlopen(req2, timeout=15) as r:
                    reprocess_status = {"code": r.status, "body": _json.loads(r.read() or b"{}")}
                    applied.append("reprocess_llm")
            except Exception as e:
                reprocess_status = {"error": str(e)}

        # Trace audit dans user_feedback (type='correction').
        try:
            fb = UserFeedback(
                user_sub=user_sub, file_id=audio_id,
                type="correction",
                payload={"old": old, "new": new, "applied": applied},
                status="new",
            )
            db.add(fb)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("correct-term: feedback trace failed (non-fatal)")

        return jsonify({
            "applied": applied,
            "reprocess": reprocess_status,
            "old": old, "new": new,
        })
    except Exception:
        db.rollback()
        logger.exception("correct-term failed")
        return jsonify({"error": "internal"}), 500
    finally:
        db.close()


@app.route("/api/v1/audio/<audio_id>/term-sources", methods=["GET"])
def term_sources(audio_id: str):
    """Retourne les segments speaker-tagged correspondant à un terme/requête.

    **Deux modes** :

    - ``term=<str>`` : recherche par sous-chaîne exacte (1 mot ou expression).
      Tous les segments contenant la sous-chaîne (case-insensitive) sont
      retournés, triés par ordre d'apparition. Utilisé par le drawer "Sources"
      depuis une pastille 🔍 sur un terme corrigé.

    - ``query=<str>`` : recherche fuzzy multi-mots (phrase). Tokenise la
      requête en mots ≥ 3 caractères (stopwords FR filtrés), calcule pour
      chaque segment un score = nombre de mots distinctifs trouvés
      (insensible à la casse). Retourne les top 8 segments par score
      décroissant. Utilisé par le bouton 🔍 par ligne CR (option C : trouver
      la source brute d'une ligne du compte-rendu, qui peut avoir été
      reformulée par l'LLM).

    Query params : ``user_sub`` (requis) + l'un de ``term`` ou ``query``.
    Réponse : ``{sources: [{idx, start, end, speaker, snippet, score?}]}``.

    Cherche dans ``speaker_tagged_text`` en priorité (avec timecodes par
    bloc, parsable). Fallback ``transcription_text`` si pas de diarisation
    (1 seul "bloc" sans timing).
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    term = (request.args.get("term") or "").strip()
    query = (request.args.get("query") or "").strip()
    user_sub = (request.args.get("user_sub") or "").strip()
    if not term and not query:
        return jsonify({"error": "term or query required"}), 400
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400
    if term and len(term) > 200:
        return jsonify({"error": "term too long"}), 400
    if query and len(query) > 1000:
        return jsonify({"error": "query too long"}), 400
    if SessionLocal is None:
        return jsonify({"error": "db_unavailable"}), 503

    from libs.shared.app.models import UserAudioFile
    db = SessionLocal()
    try:
        uaf = (
            db.query(UserAudioFile)
            .filter(UserAudioFile.id == audio_id,
                    UserAudioFile.user_sub == user_sub)
            .first()
        )
        if not uaf:
            return jsonify({"error": "not_found"}), 404
        st = uaf.speaker_tagged_text or ""
        sources = []

        # Parse blocs depuis speaker_tagged_text (format frontend identique).
        import re as _re
        head_re = _re.compile(
            r"^\*\*([^*]+)\*\*\s*_\((\d+):(\d+(?:\.\d+)?)\s*[→>-]+\s*(\d+):(\d+(?:\.\d+)?)\)_"
        )
        blocks = []
        current = None
        for raw in st.split("\n"):
            line = raw.rstrip()
            m = head_re.match(line)
            if m:
                if current is not None:
                    blocks.append(current)
                current = {
                    "speaker": m.group(1).strip(),
                    "start": int(m.group(2)) * 60 + float(m.group(3)),
                    "end":   int(m.group(4)) * 60 + float(m.group(5)),
                    "text": "",
                }
            elif current is not None and line.startswith(">"):
                t = line.lstrip(">").strip()
                current["text"] = (current["text"] + " " + t) if current["text"] else t
        if current is not None:
            blocks.append(current)

        if term:
            # Mode "term" : substring exact, retour ordre d'apparition.
            term_lower = term.lower()
            if blocks:
                for idx, b in enumerate(blocks):
                    hit = b["text"].lower().find(term_lower)
                    if hit < 0:
                        continue
                    w = 60
                    left = max(0, hit - w)
                    right = min(len(b["text"]), hit + len(term) + w)
                    snippet = (
                        ("…" if left > 0 else "")
                        + b["text"][left:hit]
                        + "«" + b["text"][hit:hit + len(term)] + "»"
                        + b["text"][hit + len(term):right]
                        + ("…" if right < len(b["text"]) else "")
                    )
                    sources.append({
                        "idx": idx,
                        "start": b["start"],
                        "end": b["end"],
                        "speaker": b["speaker"],
                        "snippet": snippet,
                    })
            elif uaf.transcription_text:
                hit = uaf.transcription_text.lower().find(term_lower)
                if hit >= 0:
                    w = 60
                    left = max(0, hit - w)
                    right = min(len(uaf.transcription_text), hit + len(term) + w)
                    snippet = (
                        ("…" if left > 0 else "")
                        + uaf.transcription_text[left:hit]
                        + "«" + uaf.transcription_text[hit:hit + len(term)] + "»"
                        + uaf.transcription_text[hit + len(term):right]
                        + ("…" if right < len(uaf.transcription_text) else "")
                    )
                    sources.append({
                        "idx": 0, "start": None, "end": None,
                        "speaker": "(transcription brute)", "snippet": snippet,
                    })
        else:
            # Mode "query" : tokenise + word-overlap score, top 8 par score
            # décroissant. Stopwords FR de base pour ne pas matcher sur
            # "le", "la", "de", etc. qui sont partout.
            STOPWORDS_FR = {
                "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou",
                "est", "sont", "que", "qui", "quoi", "ce", "cette", "ces",
                "se", "sa", "son", "ses", "leur", "leurs", "pour", "par",
                "sur", "dans", "avec", "sans", "vers", "plus", "moins",
                "tout", "tous", "toute", "toutes", "il", "ils", "elle",
                "elles", "on", "nous", "vous", "je", "tu", "me", "te", "y",
                "en", "au", "aux", "comme", "mais", "donc", "car", "alors",
                "puis", "ne", "pas", "non", "oui", "si", "déjà", "encore",
                "très", "bien", "fait", "être", "avoir", "avait", "etait",
                "été", "faire", "peut", "doit", "il", "elle", "cela", "ça",
                "ceci", "celui", "celle", "ils", "elles",
            }
            # Tokens du query : ≥ 3 chars, hors stopwords, dédupliqués.
            raw_tokens = _re.findall(r"\b[\wÀ-ÿ-]+\b", query.lower())
            q_tokens = []
            seen_q = set()
            for t in raw_tokens:
                if len(t) < 3 or t in STOPWORDS_FR:
                    continue
                if t in seen_q:
                    continue
                seen_q.add(t)
                q_tokens.append(t)
            if not q_tokens:
                return jsonify({
                    "query": query, "audio_id": audio_id,
                    "sources_count": 0, "sources": [],
                    "warning": "query trop générique (aucun mot distinctif)",
                }), 200

            scored = []
            target_blocks = blocks if blocks else (
                [{
                    "idx": 0, "speaker": "(transcription brute)",
                    "start": None, "end": None,
                    "text": uaf.transcription_text or "",
                }] if uaf.transcription_text else []
            )
            for idx, b in enumerate(target_blocks):
                text_lower = b["text"].lower()
                if not text_lower:
                    continue
                matched = [t for t in q_tokens if t in text_lower]
                score = len(matched)
                if score == 0:
                    continue
                # Snippet : on cible la 1ère occurrence du mot matché le
                # plus rare (= le plus long, heuristique). Sinon début.
                pivot_word = sorted(matched, key=len, reverse=True)[0] if matched else ""
                hit = text_lower.find(pivot_word) if pivot_word else 0
                if hit < 0:
                    hit = 0
                w = 80
                left = max(0, hit - w)
                right = min(len(b["text"]), hit + len(pivot_word) + w)
                pre = b["text"][left:hit]
                mid = b["text"][hit:hit + len(pivot_word)]
                post = b["text"][hit + len(pivot_word):right]
                snippet = (
                    ("…" if left > 0 else "")
                    + pre + "«" + mid + "»" + post
                    + ("…" if right < len(b["text"]) else "")
                )
                scored.append({
                    "idx": idx if blocks else 0,
                    "start": b.get("start"),
                    "end": b.get("end"),
                    "speaker": b["speaker"],
                    "snippet": snippet,
                    "score": score,
                    "matched_words": matched,
                })
            scored.sort(key=lambda x: (-x["score"], x.get("start") or 0))
            sources = scored[:8]

        return jsonify({
            "term": term or None,
            "query": query or None,
            "audio_id": audio_id,
            "sources_count": len(sources),
            "sources": sources,
        }), 200
    finally:
        db.close()


def create_app():
    global SessionLocal, _purge_thread_started, _pull_loop_thread_started, _orphan_resume_started, _orphan_watchdog_started
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    init_tables(db_cfg, InternalBase)
    ensure_bucket(s3_internal_cfg)
    try:
        declare_queues(rabbit_cfg)
    except Exception as e:
        logger.warning("Could not declare queues (may be normal if separate RabbitMQ): %s", e)
    SessionLocal = create_session_factory(db_cfg)
    if not _purge_thread_started:
        purge_thread = threading.Thread(target=_purge_loop, daemon=True, name="internal-purge-loop")
        purge_thread.start()
        _purge_thread_started = True
    if not _pull_loop_thread_started:
        pull_thread = threading.Thread(target=_pull_queue_loop, daemon=True, name="internal-pull-drain")
        pull_thread.start()
        _pull_loop_thread_started = True
    # Phase 2bis : reprise auto des polls Kevent orphelins (jobs en cours
    # dont le pod précédent est mort sans terminer le poll). Lancé une
    # seule fois au boot, dans un thread non bloquant.
    if not _orphan_resume_started:
        threading.Thread(target=_resume_orphaned_kevent_polls,
                         daemon=True, name="kevent-orphan-resume").start()
        _orphan_resume_started = True
    # Watchdog périodique : filet de sécurité pour les rows où kevent_job_id
    # n'a pas été persisté (race window submission→DB write) ou pour les
    # polls qui ont silencieusement crashé pendant la vie du pod.
    if not _orphan_watchdog_started:
        threading.Thread(target=_orphan_watchdog_loop,
                         daemon=True, name="kevent-orphan-watchdog").start()
        _orphan_watchdog_started = True
    if INTERNAL_PUSH_TRIGGER_TOKEN:
        logger.info("Pull trigger HTTP endpoint enabled (allowlist=%s)",
                    _TRIGGER_IP_ALLOWLIST_RAW or "<empty>")
    else:
        logger.info("Pull trigger HTTP endpoint disabled (no INTERNAL_PUSH_TRIGGER_TOKEN set)")
    return app


# WSGI entrypoint for Gunicorn. Skipped in tests via SKIP_CREATE_APP=1 so unit
# tests can import this module without spinning up DB/S3/RabbitMQ connections.
if os.getenv("SKIP_CREATE_APP", "0") != "1":
    application = create_app()
else:
    application = app


if __name__ == "__main__":
    port = int(os.getenv("FILE_PULLER_PORT", 8090))
    application.run(host="0.0.0.0", port=port)
