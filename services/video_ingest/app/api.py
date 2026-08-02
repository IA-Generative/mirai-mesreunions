"""API REST Flask du service `video-ingest`.

Endpoints (préfixe `/video` pour clarté du routing chez les clients).

| Méthode | Chemin                          | Auth   | Description                                  |
|---------|---------------------------------|--------|----------------------------------------------|
| POST    | /video/import                   | Bearer | Enqueue un job d'ingestion                   |
| GET     | /video/jobs/<id>                | Bearer | Statut d'un job                              |
| GET     | /video/sources/<id>             | Bearer | Métadonnées d'une source                     |
| GET     | /video/sources/<id>/transcript  | Bearer | Transcript préféré (langue auto)             |
| GET     | /video/search?q=…&lang=fr       | Bearer | Recherche full-text (tsvector français)      |
| DELETE  | /video/sources/<id>             | Admin  | Purge complète (cascade)                     |
| GET     | /health                         | —      | Sonde liveness                               |

Aucun appel HTTP custom : tout passe par les providers (cf. D15).
"""

from __future__ import annotations

import logging

from flask import Blueprint, Flask, g, jsonify, request

from . import audit
from . import db
from . import jobs as jobs_mod
from . import quotas
from . import repo
from .auth import require_admin, require_auth
from .providers.youtube import url as yt_url
from .providers.youtube import YouTubeProvider
from .repo import add_bookmark, find_source_by_provider_id, has_transcript, user_owns_source

log = logging.getLogger(__name__)

bp = Blueprint("video_ingest", __name__)

_PROVIDERS = [YouTubeProvider()]


def _route_url(url: str) -> tuple[str, str] | None:
    """Renvoie (provider_name, provider_video_id) si l'URL est routable."""
    for p in _PROVIDERS:
        if p.matches_url(url):
            try:
                vid, _ = p.parse_canonical_id(url)
            except Exception:
                return None
            return p.name, vid
    return None


@bp.get("/health")
def health():
    return jsonify({"status": "ok"})


@bp.post("/video/import")
@require_auth
def import_video():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url requis"}), 400

    routing = _route_url(url)
    if routing is None:
        return jsonify({"error": "URL non reconnue par les providers configurés"}), 400
    provider_name, provider_video_id = routing

    # Lookup dédup synchrone — si HIT immédiat, on renvoie reused=true sans
    # créer de job (latence < 50ms vs > 2s pour un MISS) — cf. DoD §10.
    with db.connection() as conn:
        existing = find_source_by_provider_id(
            conn, provider=provider_name, provider_video_id=provider_video_id,
        )
        language_pref = payload.get("language")
        if existing is not None and has_transcript(
            conn, video_source_id=existing, language=language_pref,
        ):
            bookmark_id = add_bookmark(
                conn, user_sub=g.user_sub, video_source_id=existing,
                context=payload.get("context"), context_id=payload.get("context_id"),
            )
            audit.log_event(
                conn, action="import", user_sub=g.user_sub, url=url,
                video_source_id=existing, reused=True, job_id=None,
                context=payload.get("context"), context_id=payload.get("context_id"),
            )
            # Hook materialize en HIT cache synchrone : sinon l'user
            # obtient une row sans CR (le worker n'est pas appelé donc
            # le hook habituel dans orchestrator.run_job ne tourne pas).
            # Best-effort : n'invalide pas la réponse 200.
            try:
                src = repo.load_source(conn, existing)
                tr = repo.load_best_transcript(conn, existing, language=language_pref)
                if src and tr:
                    from .orchestrator import notify_materialize_from_cache
                    notify_materialize_from_cache(
                        provider_name=provider_name,
                        provider_video_id=provider_video_id,
                        video_source_id=existing,
                        user_sub=g.user_sub,
                        context=payload.get("context"),
                        context_id=payload.get("context_id"),
                        source_meta=src,
                        transcript_db=tr,
                    )
            except Exception:
                log.exception("HIT cache materialize from api.py failed (non-fatal)")
            return jsonify({
                "status": "ready",
                "reused": True,
                "video_source_id": existing,
                "bookmark_id": bookmark_id,
                "job_id": None,
            }), 200

    # MISS — quota check + enqueue.
    with db.connection() as conn:
        try:
            quotas.check_import_quota(conn, user_sub=g.user_sub)
        except quotas.QuotaExceeded as e:
            audit.log_event(
                conn, action="error", user_sub=g.user_sub, url=url,
                details={"reason": "quota_exceeded", "limit": e.limit, "current": e.current},
            )
            return jsonify({"error": str(e), "limit": e.limit, "current": e.current}), 429
        job_id = jobs_mod.enqueue(
            conn, url=url, user_sub=g.user_sub,
            context=payload.get("context"), context_id=payload.get("context_id"),
            language_pref=language_pref,
            force_audio=bool(payload.get("force_audio", False)),
        )
        audit.log_event(
            conn, action="import", user_sub=g.user_sub, url=url,
            video_source_id=None, reused=False, job_id=job_id,
            context=payload.get("context"), context_id=payload.get("context_id"),
            details={"force_audio": bool(payload.get("force_audio", False))},
        )
    return jsonify({
        "status": "pending",
        "reused": False,
        "video_source_id": None,
        "job_id": job_id,
    }), 202


@bp.get("/video/my-bookmarks")
@require_auth
def list_my_bookmarks():
    """Bookmarks de l'utilisateur courant joints aux metadata de source.
    Renvoyés du plus récent au plus ancien. Limité à 50 par défaut.
    Utilisé par les clients (Mes Réunions, Mes Collections) pour afficher
    « mes vidéos web importées ».
    """
    limit = min(int(request.args.get("limit", "50")), 200)
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT b.id, b.video_source_id, b.bookmarked_at, b.context,
                   b.context_id,
                   s.provider, s.provider_video_id, s.canonical_url,
                   s.title, s.channel, s.duration_sec,
                   (SELECT t.language FROM video_transcripts t
                      WHERE t.video_source_id = s.id
                      ORDER BY (t.method = 'subtitle_manual') DESC,
                               t.created_at DESC LIMIT 1) AS lang,
                   (SELECT LENGTH(t.content_text) FROM video_transcripts t
                      WHERE t.video_source_id = s.id
                      ORDER BY (t.method = 'subtitle_manual') DESC,
                               t.created_at DESC LIMIT 1) AS chars,
                   (SELECT t.method FROM video_transcripts t
                      WHERE t.video_source_id = s.id
                      ORDER BY (t.method = 'subtitle_manual') DESC,
                               t.created_at DESC LIMIT 1) AS method
              FROM user_video_bookmarks b
              JOIN video_sources s ON s.id = b.video_source_id
             WHERE b.user_sub = %s
             ORDER BY b.bookmarked_at DESC
             LIMIT %s
            """,
            (g.user_sub, limit),
        )
        rows = cur.fetchall()
    return jsonify({
        "bookmarks": [
            {
                "bookmark_id": r[0],
                "video_source_id": r[1],
                "bookmarked_at": r[2].isoformat() if r[2] else None,
                "context": r[3],
                "context_id": r[4],
                "provider": r[5],
                "provider_video_id": r[6],
                "canonical_url": r[7],
                "title": r[8],
                "channel": r[9],
                "duration_sec": r[10],
                "transcript_language": r[11],
                "transcript_chars": r[12],
                "transcript_method": r[13],
                "has_transcript": r[11] is not None,
            }
            for r in rows
        ],
    })


@bp.get("/video/jobs/<int:job_id>")
@require_auth
def get_job(job_id: int):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, video_source_id, reused, error_message,
                   attempts, created_at, completed_at, user_sub,
                   next_attempt_at
              FROM video_ingest_jobs WHERE id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "job introuvable"}), 404
    # Un user ne voit que ses jobs (sauf admin — V1.5).
    if row[8] != g.user_sub:
        return jsonify({"error": "job introuvable"}), 404
    return jsonify({
        "id": row[0], "status": row[1], "video_source_id": row[2],
        "reused": row[3], "error_message": row[4], "attempts": row[5],
        "created_at": row[6].isoformat() if row[6] else None,
        "completed_at": row[7].isoformat() if row[7] else None,
        # Renseigné quand le job est en backoff après un échec transitoire
        # (anti-bot YouTube) : permet au front d'afficher « nouvelle
        # tentative… » plutôt qu'un « en cours » qui semble figé.
        "next_attempt_at": row[9].isoformat() if row[9] else None,
        "retrying": bool(row[9]) and row[1] == "pending",
    })


@bp.get("/video/sources/<int:source_id>")
@require_auth
def get_source(source_id: int):
    with db.connection() as conn:
        # Contrôle de propriété : le catalogue est un cache partagé, on ne
        # révèle une source qu'aux utilisateurs qui y ont un lien légitime
        # (signet ou job). Sinon 404 — pas d'énumération du catalogue global.
        if not user_owns_source(conn, user_sub=g.user_sub, video_source_id=source_id):
            return jsonify({"error": "source introuvable"}), 404
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, provider, provider_video_id, canonical_url, title,
                       channel, duration_sec, published_at, metadata_json, fetched_at
                  FROM video_sources WHERE id = %s
                """,
                (source_id,),
            )
            row = cur.fetchone()
    if not row:
        return jsonify({"error": "source introuvable"}), 404
    return jsonify({
        "id": row[0], "provider": row[1], "provider_video_id": row[2],
        "canonical_url": row[3], "title": row[4], "channel": row[5],
        "duration_sec": row[6],
        "published_at": row[7].isoformat() if row[7] else None,
        "metadata": row[8] or {},
        "fetched_at": row[9].isoformat() if row[9] else None,
    })


@bp.get("/video/sources/<int:source_id>/transcript")
@require_auth
def get_transcript(source_id: int):
    language = request.args.get("language")
    format_ = request.args.get("format", "text")  # text | segments | markdown
    with db.connection() as conn:
        # Même contrôle de propriété que get_source : le transcript d'une
        # source du catalogue partagé n'est lisible que par un utilisateur
        # qui y a un lien légitime (signet ou job).
        if not user_owns_source(conn, user_sub=g.user_sub, video_source_id=source_id):
            return jsonify({"error": "transcript introuvable"}), 404
        with conn.cursor() as cur:
            if language:
                cur.execute(
                    """SELECT id, language, method, content_text, segments_json
                         FROM video_transcripts
                        WHERE video_source_id = %s AND language = %s
                        ORDER BY (method = 'subtitle_manual') DESC, created_at DESC
                        LIMIT 1""",
                    (source_id, language),
                )
            else:
                cur.execute(
                    """SELECT id, language, method, content_text, segments_json
                         FROM video_transcripts
                        WHERE video_source_id = %s
                        ORDER BY (method = 'subtitle_manual') DESC, created_at DESC
                        LIMIT 1""",
                    (source_id,),
                )
            row = cur.fetchone()
    if not row:
        return jsonify({"error": "transcript introuvable"}), 404
    base = {"id": row[0], "language": row[1], "method": row[2]}
    if format_ == "segments":
        base["segments"] = row[4] or []
    elif format_ == "markdown":
        base["markdown"] = _render_markdown(row[3], row[4] or [])
    else:
        base["text"] = row[3]
    return jsonify(base)


def _render_markdown(text: str, segments: list[dict]) -> str:
    """Markdown horodaté minimaliste, un paragraphe par chunk avec ancre `?t=`."""
    if not segments:
        return text
    parts = []
    for s in segments:
        ts = int(s.get("start_seconds", 0))
        parts.append(f"**[{ts}s]** {s.get('text', '')}".strip())
    return "\n\n".join(parts)


@bp.get("/video/search")
@require_auth
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "paramètre q requis"}), 400
    limit = min(int(request.args.get("limit", "20")), 100)
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT t.video_source_id, s.title, s.canonical_url, t.language,
                   ts_rank(t.content_tsv, plainto_tsquery('french', %s)) AS rank,
                   ts_headline('french', t.content_text, plainto_tsquery('french', %s),
                               'MaxFragments=2,MaxWords=30') AS snippet
              FROM video_transcripts t
              JOIN video_sources s ON s.id = t.video_source_id
             WHERE t.content_tsv @@ plainto_tsquery('french', %s)
             ORDER BY rank DESC
             LIMIT %s
            """,
            (q, q, q, limit),
        )
        rows = cur.fetchall()
    return jsonify({
        "query": q,
        "results": [
            {"video_source_id": r[0], "title": r[1], "canonical_url": r[2],
             "language": r[3], "rank": float(r[4]), "snippet": r[5]}
            for r in rows
        ],
    })


@bp.delete("/video/sources/<int:source_id>")
@require_admin
def purge_source(source_id: int):
    """Purge d'une source et de tout son sillage (transcripts, bookmarks,
    jobs liés). Cascade gérée par les FK ON DELETE CASCADE de la migration
    019 pour transcripts et bookmarks ; les jobs sont mis à NULL (ON
    DELETE SET NULL) pour préserver l'audit."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM video_sources WHERE id = %s RETURNING id", (source_id,))
            row = cur.fetchone()
        if row:
            audit.log_event(
                conn, action="purge", user_sub=g.user_sub,
                video_source_id=source_id,
            )
    if not row:
        return jsonify({"error": "source introuvable"}), 404
    log.warning("purge admin: source %s supprimée par %s", source_id, g.user_sub)
    return jsonify({"status": "purged", "video_source_id": source_id})


def create_app() -> Flask:
    # Fail-fast : si VIDEO_INGEST_MATERIALIZE_REQUIRED=true, on refuse de
    # booter sans l'URL materialize + token. Évite le faux positif de
    # succès observé en prod-bêta (HIT cache répond reused=true mais la
    # row n'apparaît jamais dans Mes Réunions, parce que le hook est
    # silencieusement skippé). En mode standalone D14, la var reste
    # absente → comportement best-effort historique.
    import os
    if os.environ.get("VIDEO_INGEST_MATERIALIZE_REQUIRED", "").lower() == "true":
        missing = [
            k for k in ("VIDEO_INGEST_MATERIALIZE_URL", "VIDEO_INGEST_INTERNAL_API_TOKEN")
            if not os.environ.get(k)
        ]
        if missing:
            raise RuntimeError(
                "VIDEO_INGEST_MATERIALIZE_REQUIRED=true mais env var(s) "
                f"manquante(s) : {', '.join(missing)}. Refuse de booter "
                "pour éviter les imports silencieusement non matérialisés."
            )
    # Garde de démarrage fail-closed : audience obligatoire + refus de
    # désactivation d'auth en production.
    from .auth import assert_startup_auth_config
    assert_startup_auth_config()
    app = Flask("video_ingest")
    app.register_blueprint(bp)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="0.0.0.0", port=8000)
