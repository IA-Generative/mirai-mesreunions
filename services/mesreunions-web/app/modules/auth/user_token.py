"""Jeton d'accès de la personne, relayé serveur→serveur.

Les proxys de mesreunions-web qui parlent à un service tiers AU NOM de la
personne (import YouTube vers video-ingest, agents MirAI vers Mes agents)
lisent ici son jeton d'accès Keycloak et le rafraîchissent silencieusement
quand le service tiers répond 401. Patron hérité de ``youtube_import`` et
partagé ici pour qu'un second consommateur ne le recopie pas.

- Le jeton vit dans le dépôt ``web_session_tokens`` (``token_store``) ; la
  clé de session héritée (cookies posés avant la migration) reste lue en
  repli.
- Le rafraîchissement est « au mieux » : aucun jeton n'est journalisé, seul
  le succès ou l'échec l'est. Keycloak fait tourner le refresh_token : la
  nouvelle valeur est réécrite dans le dépôt.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests as req
from flask import session

from app.modules.auth import token_store

logger = logging.getLogger("mesreunions_web.auth.user_token")


def access_token() -> Optional[str]:
    """Jeton d'accès de la personne connectée, ou None."""
    return token_store.load_tokens().get("access_token") or session.get("access_token") or None


def refresh_access_token() -> bool:
    """Tente un rafraîchissement silencieux via le refresh_token stocké.

    Retourne True si un nouveau jeton d'accès a été obtenu et écrit, False
    sinon (pas de refresh_token, Keycloak en erreur, dépôt indisponible).
    """
    rt = token_store.load_tokens().get("refresh_token") or session.get("refresh_token")
    if not rt:
        return False
    try:
        from libs.shared.app.config import OIDCConfig  # noqa: E402
        cfg = OIDCConfig()
        token_url = cfg.issuer.rstrip("/") + "/protocol/openid-connect/token"
        resp = req.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
            },
            timeout=8,
        )
        if resp.status_code != 200:
            logger.warning("OIDC refresh HTTP %d", resp.status_code)
            return False
        body = resp.json()
        new_at = body.get("access_token")
        if not new_at:
            return False
        updated = token_store.update_tokens(
            access_token=new_at,
            refresh_token=body.get("refresh_token"),
        )
        if not updated:
            # Session héritée (cookie d'avant la migration) : on garde
            # l'ancien emplacement pour ne pas casser la requête en cours.
            session["access_token"] = new_at
            if body.get("refresh_token"):
                session["refresh_token"] = body["refresh_token"]
        logger.info("OIDC access_token refreshed silently")
        return True
    except Exception:
        logger.exception("OIDC refresh failed (non-fatal)")
        return False
