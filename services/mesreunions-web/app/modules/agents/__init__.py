"""Module agents — contrat d'agents MirAI (Mes agents) sur une réunion.

Expose, derrière la session (``require_auth``) :
  GET  /api/agents?input=meeting                          la liste de la personne
  POST /api/meetings/<meeting_id>/agents/<agent_id>/run   lance un agent sur un texte

Le jeton d'accès de la personne est relayé à Mes agents côté serveur
(``modules/auth/user_token.py``), jamais exposé au navigateur.
"""
from .routes import bp as agents_bp

__all__ = ["agents_bp"]
