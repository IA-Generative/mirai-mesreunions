"""Identité utilisateur fournie par Passage2 (SSO du ministère de l'Intérieur).

Passage2 est un reverse proxy authentifiant (Apache + mod_auth_mellon, SAML) :
il authentifie l'agent puis transmet la requête au service en ajoutant des
en-têtes (``Mellon-NameID``, ``Mellon-Groups``, ``Mellon-Role``…). Le service
ne fait AUCUNE authentification lui-même : il lit ces en-têtes.

Ces en-têtes ne valent que s'ils viennent du proxy. Le service doit donc
refuser toute requête qui ne l'a pas traversé, par au moins un des deux
mécanismes suivants (fail-closed hors développement) :

- ``PASSAGE2_PROXY_SECRET`` : secret partagé que le proxy ajoute dans
  ``PASSAGE2_PROXY_SECRET_HEADER`` (``RequestHeader set`` côté Apache) ;
- ``PASSAGE2_TRUSTED_PROXIES`` : CIDR des adresses autorisées à appeler le
  service (le proxy), comparées à l'adresse TCP du pair.

Variables d'environnement (toutes optionnelles sauf le mécanisme de confiance) :

- ``PASSAGE2_USER_HEADER`` (``Mellon-NameID``) : identifiant de l'agent ;
- ``PASSAGE2_EMAIL_HEADER`` (``Mellon-NameID``) : adresse e-mail, utilisée
  pour retrouver le compte Keycloak ;
- ``PASSAGE2_NAME_HEADER`` (vide) : nom affiché ;
- ``PASSAGE2_GROUPS_HEADER`` (``Mellon-Groups``) et
  ``PASSAGE2_REQUIRED_GROUPS`` (vide = tout agent authentifié) : restriction
  d'accès à un ou plusieurs groupes ;
- ``PASSAGE2_DEV_IDENTITY`` : e-mail injecté quand ``ENVIRONMENT=development``
  et qu'aucun proxy n'est présent (poste de développement uniquement).
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

from libs.shared.app.security import is_strong_shared_secret

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Passage2StartupError(RuntimeError):
    """Configuration qui ouvrirait le service sans passer par Passage2."""


class Passage2Error(Exception):
    """Requête refusée : ``code`` est stable (affichage, tests), ``status`` HTTP."""

    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Passage2Identity:
    name_id: str
    email: str
    display_name: str
    groups: Tuple[str, ...] = ()


def _split_groups(raw: str) -> Tuple[str, ...]:
    return tuple(g.strip() for g in re.split(r"[,;]", raw or "") if g.strip())


@dataclass(frozen=True)
class Passage2Config:
    environment: str = "production"
    user_header: str = "Mellon-NameID"
    email_header: str = "Mellon-NameID"
    name_header: str = ""
    groups_header: str = "Mellon-Groups"
    required_groups: Tuple[str, ...] = ()
    proxy_secret_header: str = "X-Passage2-Proxy-Secret"
    proxy_secret: str = ""
    trusted_proxies: Tuple[ipaddress._BaseNetwork, ...] = field(default_factory=tuple)
    dev_identity: str = ""

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Passage2Config":
        env = os.environ if env is None else env

        def get(key: str, default: str = "") -> str:
            return (env.get(key) or default).strip()

        try:
            proxies = tuple(
                ipaddress.ip_network(cidr, strict=False)
                for cidr in _split_groups(get("PASSAGE2_TRUSTED_PROXIES"))
            )
        except ValueError as exc:
            raise Passage2StartupError(f"PASSAGE2_TRUSTED_PROXIES invalide : {exc}") from exc

        cfg = cls(
            environment=get("ENVIRONMENT", "production").lower(),
            user_header=get("PASSAGE2_USER_HEADER", "Mellon-NameID"),
            email_header=get("PASSAGE2_EMAIL_HEADER", "Mellon-NameID"),
            name_header=get("PASSAGE2_NAME_HEADER"),
            groups_header=get("PASSAGE2_GROUPS_HEADER", "Mellon-Groups"),
            required_groups=_split_groups(get("PASSAGE2_REQUIRED_GROUPS")),
            proxy_secret_header=get("PASSAGE2_PROXY_SECRET_HEADER", "X-Passage2-Proxy-Secret"),
            proxy_secret=get("PASSAGE2_PROXY_SECRET"),
            trusted_proxies=proxies,
            dev_identity=get("PASSAGE2_DEV_IDENTITY"),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Refuse une configuration qui accepterait des en-têtes forgés."""
        if self.proxy_secret and not is_strong_shared_secret(self.proxy_secret):
            raise Passage2StartupError(
                "PASSAGE2_PROXY_SECRET trop faible (32 caractères minimum, pas de valeur par défaut)."
            )
        if self.dev_identity and not self.is_development:
            raise Passage2StartupError(
                "PASSAGE2_DEV_IDENTITY n'est autorisé qu'avec ENVIRONMENT=development."
            )
        if not self.is_development and not (self.proxy_secret or self.trusted_proxies):
            raise Passage2StartupError(
                "Aucun mécanisme de confiance Passage2 : définir PASSAGE2_PROXY_SECRET "
                "et/ou PASSAGE2_TRUSTED_PROXIES."
            )


def _from_trusted_proxy(cfg: Passage2Config, headers: Mapping[str, str],
                        remote_addr: Optional[str]) -> bool:
    if cfg.proxy_secret:
        presented = headers.get(cfg.proxy_secret_header) or ""
        if not hmac.compare_digest(presented.encode(), cfg.proxy_secret.encode()):
            return False
    if cfg.trusted_proxies:
        try:
            peer = ipaddress.ip_address((remote_addr or "").strip())
        except ValueError:
            return False
        if not any(peer in net for net in cfg.trusted_proxies):
            return False
    return True


def read_identity(cfg: Passage2Config, headers: Mapping[str, str],
                  remote_addr: Optional[str]) -> Passage2Identity:
    """Extrait l'identité Passage2 d'une requête, ou lève ``Passage2Error``."""
    name_id = (headers.get(cfg.user_header) or "").strip()

    if not name_id and cfg.is_development and cfg.dev_identity:
        return Passage2Identity(name_id=cfg.dev_identity, email=cfg.dev_identity.lower(),
                                display_name=cfg.dev_identity)

    if not _from_trusted_proxy(cfg, headers, remote_addr):
        raise Passage2Error("untrusted_proxy", 403,
                            "Accès refusé : passez par le portail Passage2.")
    if not name_id:
        raise Passage2Error("not_authenticated", 401,
                            "Identité Passage2 absente : reconnectez-vous.")

    email = (headers.get(cfg.email_header) or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise Passage2Error("email_missing", 403,
                            "Passage2 ne transmet pas d'adresse e-mail pour ce compte.")

    groups = _split_groups(headers.get(cfg.groups_header) or "") if cfg.groups_header else ()
    if cfg.required_groups and not set(cfg.required_groups) & set(groups):
        raise Passage2Error("forbidden_group", 403,
                            "Votre compte n'est pas autorisé à enrôler un appareil.")

    display_name = (headers.get(cfg.name_header) or "").strip() if cfg.name_header else ""
    return Passage2Identity(name_id=name_id, email=email,
                            display_name=display_name or email, groups=groups)
