"""Module auth — routes OIDC ``/login``, ``/auth/callback``, ``/logout``.

PR3-v2 : les routes sont extraites ici. L'init ``oauth.register(...)`` reste
dans ``main.py`` au module-level (exigence flask-oauthlib).

Les helpers transverses (``get_current_user``, ``require_auth``) sont dans
``app/shared.py`` et utilisés par tous les blueprints.
"""

from .routes import bp as auth_bp

__all__ = ["auth_bp"]
