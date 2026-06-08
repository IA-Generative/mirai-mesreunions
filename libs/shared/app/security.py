"""Shared security helpers for internal API authentication."""

import hmac
import os
from typing import Optional


WEAK_MARKERS = (
    "changeme",
    "change-me",
    "default",
    "example",
    "dev-",
    "test-",
    "dummy",
)


def parse_bearer_token(header_value: str) -> Optional[str]:
    """Parse a standard Authorization Bearer header."""
    if not header_value or not header_value.startswith("Bearer "):
        return None
    token = header_value.split(" ", 1)[1].strip()
    return token or None


def verify_bearer_token(header_value: str, expected_token: str) -> bool:
    """Constant-time Bearer token verification."""
    token = parse_bearer_token(header_value)
    if not token or not expected_token:
        return False
    return hmac.compare_digest(token, expected_token)


def is_strong_shared_secret(value: str) -> bool:
    """Minimal policy for internal shared secrets used across services."""
    if not value or len(value) < 32:
        return False
    lowered = value.lower()
    if any(marker in lowered for marker in WEAK_MARKERS):
        return False
    return True


def require_strong_shared_secret(env_key: str) -> str:
    """
    Ensure a strong secret exists in env.
    Raises RuntimeError if missing/weak to fail fast at startup.
    """
    value = os.getenv(env_key, "")
    if not is_strong_shared_secret(value):
        raise RuntimeError(
            f"{env_key} is missing or too weak. "
            "Use at least 32 chars and avoid placeholders/default/test values."
        )
    return value


def resolve_auto_transcribe(requested, *, is_admin: bool = False, env=None) -> bool:
    """Politique serveur d'activation du traitement coûteux (Whisper + LLM).

    L'activation ne doit pas être pilotée librement par l'utilisateur final
    (abus de ressources / déni de service économique sur une chaîne GPU).
    La décision est prise **côté serveur** via ``AUTO_TRANSCRIBE_POLICY`` :

      - ``"off"`` (défaut sûr) : jamais activé, la valeur du payload est
        ignorée ;
      - ``"admin"`` : activable uniquement par un rôle admin (``is_admin``) ;
      - ``"user"`` : l'utilisateur choisit (UX historique, opt-in explicite) ;
      - ``"on"`` : toujours activé côté serveur.

    Appliquée de façon **identique** aux deux points d'émission du jeton
    (zone externe et autorité interne) pour rester cohérente.
    """
    policy = (env or os.environ).get("AUTO_TRANSCRIBE_POLICY", "off")
    policy = (policy or "off").strip().lower()
    if policy == "user":
        return bool(requested)
    if policy == "admin":
        return bool(requested) and bool(is_admin)
    if policy == "on":
        return True
    return False

