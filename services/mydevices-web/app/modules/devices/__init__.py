"""Module devices — TODO PR3 v2 : extraire QR / enroll / retention / my-devices.

Routes restant à déplacer depuis ``app/main.py`` :
- ``POST /api/generate-code``
- ``GET  /api/qr-image/<qr_token>``
- ``GET  /api/my-devices``
- ``POST /api/my-devices/<id>/rename`` / ``revoke`` / ``DELETE``
- ``POST /api/my-devices/revoke-all``
- ``POST /api/my-token/renew-7d``
- ``POST /api/device/enroll-proxy`` / ``validate-proxy``
"""
