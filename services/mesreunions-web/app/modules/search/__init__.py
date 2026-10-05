"""Module search — contrat de recherche MirAI (Mon portail).

Expose, hors session cookie (Bearer OIDC, CORS limité) :
  GET     /api/v1/search
  OPTIONS /api/v1/search   (préflight CORS)
"""
from .routes import bp as search_bp

__all__ = ["search_bp"]
