"""Module feedback — pouce ↑/↓ + demandes de regénération.

Cf. migration 015 + tables/user_feedback. Expose 5 routes via blueprint :
  POST   /api/file/<file_id>/feedback
  POST   /api/feedback
  GET    /api/my-feedback
  GET    /api/admin/feedback
  PATCH  /api/admin/feedback/<feedback_id>
  POST   /api/file/<file_id>/regenerate         (Phase 2)
"""
from .routes import bp as feedback_bp

__all__ = ["feedback_bp"]
