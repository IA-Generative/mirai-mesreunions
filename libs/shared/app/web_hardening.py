"""Durcissement HTTP applicatif partagé (en-têtes + drapeaux de cookie).

Les en-têtes HSTS/CSP/X-Frame sont posés à l'ingress (cf. audit infra). Ce
module complète le résidu côté application : en-tête ``Permissions-Policy``
manquant et drapeaux de cookie de session (``HttpOnly``/``SameSite``/
``Secure``). Centralisé pour être appliqué identiquement à chaque app Flask.
"""

from __future__ import annotations

DEFAULT_PERMISSIONS_POLICY = "geolocation=(), microphone=(self), camera=()"


def apply_security_headers(app, *, permissions_policy: str = DEFAULT_PERMISSIONS_POLICY):
    """Configure les drapeaux de cookie de session et pose les en-têtes
    de durcissement résiduels sur chaque réponse.

    ``Secure`` est activé en production (HTTPS) et relâché en dev/local
    (HTTP) pour ne pas casser le développement.
    """
    import os

    from .oidc_auth import is_production

    prod = is_production()
    # Affectation explicite : ces clés ont déjà une valeur par défaut dans
    # Flask (notamment SECURE=False), donc setdefault ne suffirait pas.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    secure_override = os.getenv("SESSION_COOKIE_SECURE")
    if secure_override is not None:
        app.config["SESSION_COOKIE_SECURE"] = secure_override.strip().lower() in {"1", "true", "yes", "on"}
    else:
        app.config["SESSION_COOKIE_SECURE"] = prod

    @app.after_request
    def _set_security_headers(resp):  # noqa: ANN001
        # setdefault-like : ne pas écraser un en-tête éventuellement posé en amont.
        if "Permissions-Policy" not in resp.headers:
            resp.headers["Permissions-Policy"] = permissions_policy
        if "X-Content-Type-Options" not in resp.headers:
            resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    return app
