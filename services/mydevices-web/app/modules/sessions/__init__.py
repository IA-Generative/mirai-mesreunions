"""Module sessions — uploads, fichiers, corbeille, status, transcript downloads.

PR3-v2 : extrait depuis main.py. Le blueprint est registré dans
``create_app()`` (cf ``main.py``).

Routes exposées (URLs canoniques préservées) :
- ``GET /api/my-sessions``, ``DELETE /api/my-sessions/<code>``, ``restore``
- ``POST /api/my-upload`` (upload local sans QR)
- ``GET /api/my-trash`` + ``restore`` / ``permanently`` sur fichiers
- ``GET /api/file/transcript/...`` / ``meeting-cr`` / download / stream
- ``GET /api/file/transcript-status/<id>``
- ``GET /api/file/normalization-impact/<id>``
- ``POST /api/file/<id>/rename`` / ``PATCH meeting-datetime``
- ``POST /api/purge-my-sessions``
- ``GET /api/queue-status`` (auth dual session OIDC + bearer interne)
"""

from .routes import bp as sessions_bp

__all__ = ["sessions_bp"]
