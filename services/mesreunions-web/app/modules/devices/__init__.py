"""Module devices — QR / enroll / retention / my-devices.

PR3-v2 : extrait depuis main.py.

Routes (URLs canoniques préservées) :
- ``POST /api/generate-code``
- ``GET  /api/qr-image/<qr_token>``
- ``GET  /api/my-devices``
- ``POST /api/my-devices/<id>/rename`` / ``revoke`` / ``DELETE``
- ``POST /api/my-devices/revoke-all``
- ``POST /api/my-token/renew-7d``
- ``POST /api/my-sessions/<session_id>/renew-7d``
- ``POST /api/device/enroll-proxy`` / ``validate-proxy``
"""

from .routes import bp as devices_bp

__all__ = ["devices_bp"]
