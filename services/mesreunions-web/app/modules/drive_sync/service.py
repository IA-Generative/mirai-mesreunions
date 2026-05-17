"""Service drive_sync — réexport façade.

L'implémentation concrète reste dans ``app/drive_brief_sync.py`` (où elle
est testée par ``tests/unit/test_drive_brief_sync_helpers.py`` qui charge
ce fichier par path absolu). Ce service expose l'API publique pour les
autres modules (``preparations`` notamment), conformément à l'architecture
PR3-v2 : les modules ne consomment ``drive_brief_sync`` qu'à travers
``app.modules.drive_sync``.

Le déplacement physique du fichier nécessiterait d'adapter le test
helper (chargement par path) — choix conservateur ici : on garde le
fichier à plat, on isole l'accès via le module.
"""

from app.drive_brief_sync import (  # noqa: F401
    schedule_drive_brief_sync,
    brief_json_to_markdown,
    documents_source_to_markdown,
)

__all__ = [
    "schedule_drive_brief_sync",
    "brief_json_to_markdown",
    "documents_source_to_markdown",
]
