"""Résolution e-mail → compte Keycloak (``sub``) via l'API d'administration.

Les fichiers déposés par un appareil sont rattachés au ``sub`` Keycloak de
son propriétaire (c'est ce que voit mes-réunions). Quand l'utilisateur
s'authentifie par un autre canal (Passage2), on retrouve son compte par
l'adresse e-mail, en correspondance exacte.

Le client Keycloak utilisé (``KEYCLOAK_ADMIN_CLIENT_ID``) est un client
confidentiel à compte de service, avec le seul rôle ``realm-management /
view-users`` sur le realm des utilisateurs.

Variables : ``KEYCLOAK_BASE_URL`` (ex. ``http://keycloak:8080``),
``KEYCLOAK_REALM`` (``openwebui``), ``KEYCLOAK_ADMIN_CLIENT_ID``,
``KEYCLOAK_ADMIN_CLIENT_SECRET``.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

import requests as req


class KeycloakLookupError(Exception):
    """Keycloak injoignable ou réponse inattendue (erreur transitoire)."""


class AmbiguousUserError(Exception):
    """Plusieurs comptes actifs partagent l'adresse e-mail."""


class KeycloakUserResolver:
    def __init__(self, base_url: str, realm: str, client_id: str, client_secret: str,
                 *, http=req, cache_ttl_seconds: int = 300,
                 clock: Callable[[], float] = time.monotonic):
        if not (base_url and realm and client_id and client_secret):
            raise ValueError("Configuration Keycloak incomplète.")
        self._base = base_url.rstrip("/")
        self._realm = realm
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._ttl = cache_ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._cache: dict = {}

    @classmethod
    def from_env(cls) -> "KeycloakUserResolver":
        return cls(
            os.getenv("KEYCLOAK_BASE_URL", "http://keycloak:8080"),
            os.getenv("KEYCLOAK_REALM", "openwebui"),
            os.getenv("KEYCLOAK_ADMIN_CLIENT_ID", ""),
            os.getenv("KEYCLOAK_ADMIN_CLIENT_SECRET", ""),
        )

    def _admin_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._token_expiry:
                return self._token
        try:
            resp = self._http.post(
                f"{self._base}/realms/{self._realm}/protocol/openid-connect/token",
                data={"grant_type": "client_credentials", "client_id": self._client_id,
                      "client_secret": self._client_secret},
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # transport, HTTP, JSON : tout échec est transitoire
            raise KeycloakLookupError("Jeton d'administration Keycloak indisponible.") from exc
        with self._lock:
            self._token = body["access_token"]
            # Marge de 30 s pour ne jamais présenter un jeton expiré.
            self._token_expiry = self._clock() + max(0, int(body.get("expires_in", 60)) - 30)
            return self._token

    def resolve_by_email(self, email: str) -> Optional[dict]:
        """Retourne ``{"sub", "email", "name"}`` du compte actif, ou ``None``."""
        email = (email or "").strip().lower()
        if not email:
            return None
        cached = self._cache.get(email)
        if cached and self._clock() < cached[0]:
            return cached[1]

        try:
            resp = self._http.get(
                f"{self._base}/admin/realms/{self._realm}/users",
                params={"email": email, "exact": "true", "briefRepresentation": "true"},
                headers={"Authorization": f"Bearer {self._admin_token()}"},
                timeout=10,
            )
            resp.raise_for_status()
            users = resp.json()
        except KeycloakLookupError:
            raise
        except Exception as exc:  # transport, HTTP, JSON : tout échec est transitoire
            raise KeycloakLookupError("Recherche du compte Keycloak impossible.") from exc

        active = [
            u for u in users if isinstance(u, dict) and u.get("enabled", True)
            and (u.get("email") or "").strip().lower() == email
        ]
        if len(active) > 1:
            raise AmbiguousUserError(email)
        user = None
        if active:
            u = active[0]
            full_name = " ".join(p for p in (u.get("firstName"), u.get("lastName")) if p)
            user = {"sub": u["id"], "email": email, "name": full_name or u.get("username") or email}
        # Les absences sont aussi mises en cache (brièvement) pour ne pas
        # marteler Keycloak si un agent sans compte recharge la page.
        self._cache[email] = (self._clock() + (self._ttl if user else min(self._ttl, 60)), user)
        return user
