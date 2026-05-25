"""Module youtube_import — proxy vers le service `video-ingest`.

Expose 2 routes (cf. INTEGRATION_NOTES.md §1 côté video-ingest) :
  POST /api/youtube/import          enqueue ou HIT cache sync
  GET  /api/youtube/jobs/<job_id>   statut du job

Le proxy forward l'access_token OIDC de l'utilisateur (stocké en session
au login, cf. modules/auth/routes.py) pour que video-ingest re-vérifie
le JWT côté service.
"""
from .routes import bp as youtube_import_bp

__all__ = ["youtube_import_bp"]
