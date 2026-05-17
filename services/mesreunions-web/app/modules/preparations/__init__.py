"""Module preparations — endpoints browser ``/api/preparations/*``.

Refacto PR3 : remplace les anciens ``/api/meeting-prep/*`` (alias supprimés).
Délègue à ``device-token-authority`` pour la persistance (zone interne).
"""

from .routes import bp as preparations_bp

__all__ = ["preparations_bp"]
