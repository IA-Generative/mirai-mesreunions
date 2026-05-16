"""Module drive_sync — versement Drive best-effort du brief de préparation.

Le code "métier" (génération des 4 fichiers brief.md / glossaire.txt /
documents-source.md / prompt-utilise.txt) est dans ``service.py``. L'API
publique exposée aux autres modules :

- ``schedule_drive_brief_sync(...)`` — démarre le thread daemon best-effort
- ``brief_json_to_markdown(...)``    — helper testable
- ``documents_source_to_markdown(...)``
"""

from .service import (
    schedule_drive_brief_sync,
    brief_json_to_markdown,
    documents_source_to_markdown,
)

__all__ = [
    "schedule_drive_brief_sync",
    "brief_json_to_markdown",
    "documents_source_to_markdown",
]
