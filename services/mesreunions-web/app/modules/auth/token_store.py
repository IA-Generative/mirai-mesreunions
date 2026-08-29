"""Stockage serveur des jetons OIDC de session (table ``web_session_tokens``).

Remplace le stockage des JWT dans le cookie de session Flask : le cookie
dépassait ~4093 octets (id + access + refresh tokens), les navigateurs le
jetaient silencieusement et l'utilisateur bouclait /login ↔ Keycloak sans
jamais être connecté (incident 2026-08-28). Le cookie ne porte plus qu'une
référence opaque ``token_ref`` ; les jetons vivent en postgres-external,
chiffrés Fernet quand ``OIDC_REFRESH_TOKEN_FERNET_KEY`` est configurée.

Toutes les fonctions sont best-effort : un échec DB dégrade les proxys
serveur→serveur (youtube_import) et le ``id_token_hint`` du logout, mais
ne doit JAMAIS casser le login lui-même (même philosophie que
``libs.shared.app.oidc_refresh_store``).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import g, session

from libs.shared.app.models import WebSessionToken
from libs.shared.app import secrets_crypto

from app.runtime import session_scope

logger = logging.getLogger("mesreunions_web.auth.token_store")

_SESSION_KEY = "token_ref"


def _ttl_days() -> int:
    return max(1, int(os.getenv("WEB_SESSION_TOKENS_TTL_DAYS", "14")))


def _cache_get():
    """Cache par requête dans flask.g — inerte hors contexte de requête."""
    try:
        return getattr(g, "_web_session_tokens", None)
    except RuntimeError:
        return None


def _cache_set(value) -> None:
    try:
        g._web_session_tokens = value
    except RuntimeError:
        pass


def _session_ref() -> Optional[str]:
    try:
        return session.get(_SESSION_KEY)
    except RuntimeError:
        return None


def _enc(value: Optional[str], use_crypto: bool) -> Optional[str]:
    if not value:
        return None
    return secrets_crypto.encrypt(value) if use_crypto else value


def _dec(value: Optional[str], was_encrypted: bool) -> Optional[str]:
    if not value:
        return None
    if not was_encrypted:
        return value
    try:
        return secrets_crypto.decrypt(value)
    except Exception:
        logger.warning("token_store: decryption failed (key rotated ?) — token dropped")
        return None


def save_tokens(user_sub: str, *, id_token: str = "", access_token: str = "",
                refresh_token: Optional[str] = None) -> Optional[str]:
    """Persiste les jetons et retourne la référence à mettre en session.

    Purge au passage les rangées plus vieilles que le TTL (sessions mortes).
    Retourne None si la persistance échoue — le login continue sans store.
    """
    ref = secrets.token_urlsafe(32)
    use_crypto = secrets_crypto.is_configured()
    if not use_crypto:
        logger.warning("token_store: OIDC_REFRESH_TOKEN_FERNET_KEY absent — "
                       "jetons stockés en clair (dégradé)")
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_ttl_days())
        with session_scope() as db:
            db.query(WebSessionToken).filter(
                WebSessionToken.updated_at < cutoff).delete(synchronize_session=False)
            db.add(WebSessionToken(
                token_ref=ref,
                user_sub=user_sub or "",
                id_token=_enc(id_token, use_crypto),
                access_token=_enc(access_token, use_crypto),
                refresh_token=_enc(refresh_token, use_crypto),
                encrypted=use_crypto,
            ))
            db.commit()
        return ref
    except Exception:
        logger.exception("token_store: persistence failed (login continues without store)")
        return None


def _load_row(db, ref: str) -> Optional[WebSessionToken]:
    return db.query(WebSessionToken).filter(WebSessionToken.token_ref == ref).first()


def load_tokens(ref: Optional[str] = None) -> dict:
    """Retourne {id_token, access_token, refresh_token} (valeurs None si absentes).

    Sans ``ref``, prend celle de la session Flask courante. Mémoïsé par
    requête dans ``flask.g`` (les proxys peuvent lire plusieurs fois).
    """
    ref = ref or _session_ref()
    if not ref:
        return {}
    cache = _cache_get()
    if cache is not None and cache.get("_ref") == ref:
        return cache
    try:
        with session_scope() as db:
            row = _load_row(db, ref)
            if row is None:
                return {}
            out = {
                "_ref": ref,
                "id_token": _dec(row.id_token, row.encrypted),
                "access_token": _dec(row.access_token, row.encrypted),
                "refresh_token": _dec(row.refresh_token, row.encrypted),
            }
    except Exception:
        logger.exception("token_store: load failed")
        return {}
    _cache_set(out)
    return out


def update_tokens(ref: Optional[str] = None, *, access_token: Optional[str] = None,
                  refresh_token: Optional[str] = None) -> bool:
    """Met à jour access/refresh après un refresh silencieux Keycloak."""
    ref = ref or _session_ref()
    if not ref:
        return False
    use_crypto = secrets_crypto.is_configured()
    try:
        with session_scope() as db:
            row = _load_row(db, ref)
            if row is None:
                return False
            if access_token is not None:
                row.access_token = _enc(access_token, use_crypto)
            if refresh_token is not None:
                row.refresh_token = _enc(refresh_token, use_crypto)
            row.encrypted = use_crypto
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
        _cache_set(None)
        return True
    except Exception:
        logger.exception("token_store: update failed")
        return False


def delete_tokens(ref: Optional[str] = None) -> None:
    """Supprime la rangée (logout)."""
    ref = ref or _session_ref()
    if not ref:
        return
    try:
        with session_scope() as db:
            db.query(WebSessionToken).filter(
                WebSessionToken.token_ref == ref).delete(synchronize_session=False)
            db.commit()
        _cache_set(None)
    except Exception:
        logger.exception("token_store: delete failed")
