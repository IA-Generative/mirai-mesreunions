"""Module meetings — endpoints browser ``/api/meetings/*``.

Refacto PR3 : nouveau périmètre browser-facing pour le CR post-réunion.
"""

from .routes import bp as meetings_bp

__all__ = ["meetings_bp"]
