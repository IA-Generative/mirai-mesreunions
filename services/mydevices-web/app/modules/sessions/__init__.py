"""Module sessions — TODO PR3 v2 : extraire uploads + corbeille + status.

Routes restant à déplacer depuis ``app/main.py`` :
- ``GET /api/my-sessions``, ``DELETE /api/my-sessions/<code>``, ``restore``
- ``POST /api/my-upload`` (upload direct)
- ``GET /api/my-trash`` + ``restore`` / ``permanently`` sur fichiers
- ``GET /api/file/transcript/...`` / ``meeting-cr`` / download / stream
- ``GET /api/file/transcript-status/<id>``
- ``GET /api/file/normalization-impact/<id>``
- ``POST /api/file/<id>/rename`` / ``meeting-datetime`` / ``reprocess``
- ``POST /api/purge-my-sessions``
"""
