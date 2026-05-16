"""Module auth — TODO PR3 v2 : extraire OIDC + session + refresh.

À ce stade (PR3), le code OIDC reste dans ``app/main.py`` (init Flask, OAuth,
routes ``/login`` / ``/auth/callback`` / ``/logout``). Voir le rapport
``docs/refactor-mydevices-report-pr3.md`` pour la justification.

Les helpers transverses (``get_current_user``, ``require_auth``) sont déjà
exposés dans ``app/shared.py`` et utilisés par les blueprints extraits.
"""
