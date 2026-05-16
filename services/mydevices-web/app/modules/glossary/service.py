"""Service glossary — wrap les appels device-token-authority pour user_glossary_terms.

L'extraction des termes depuis un brief reste hébergée dans le module
``dmz-to-internal-bridge/app/glossary_from_brief.py`` (source de vérité ;
réutilisée par le file-puller aussi). On la charge dynamiquement via
``importlib`` faute de pouvoir l'installer comme dépendance Python sans
restructurer ``services/``.
"""

from __future__ import annotations

import importlib.util as _iu
import logging
import os
from typing import Iterable

from ...shared import request_internal_device_api

logger = logging.getLogger("mydevices_web.glossary")


def extract_terms_from_brief(brief: dict, documents: list) -> list[dict]:
    """Délègue à ``dmz-to-internal-bridge.glossary_from_brief``.

    Retourne une liste de dicts ``{term, definition?, …}``. Liste vide si
    le module est introuvable (déploiement transitoire ou tests unitaires
    sans le service dmz-to-internal-bridge).
    """
    try:
        spec = _iu.spec_from_file_location(
            "_gfb_module",
            os.path.join(
                os.path.dirname(__file__),
                "..", "..", "..", "..",
                "dmz-to-internal-bridge", "app", "glossary_from_brief.py",
            ),
        )
        if spec is None or spec.loader is None:
            return []
        gfb = _iu.module_from_spec(spec)
        spec.loader.exec_module(gfb)
        return list(gfb.extract_full_glossary_terms_from_brief(brief, documents))
    except Exception:
        logger.exception("glossary: extract_terms_from_brief failed")
        return []


def upsert_terms_for_user(
    user_sub: str,
    terms: Iterable[dict],
    *,
    source_preparation_id: str | None = None,
) -> None:
    """Upsert batch des termes du glossaire utilisateur global.

    Best-effort : log et avale les exceptions. Appelée depuis le module
    ``preparations`` après création / amendement d'un brief.
    """
    terms = list(terms)
    if not terms:
        return
    try:
        request_internal_device_api(
            "POST",
            "/api/v1/user-glossary/upsert-batch",
            json_body={
                "user_sub": user_sub,
                "terms": terms,
                "source_preparation_id": source_preparation_id,
            },
        )
    except Exception:
        logger.exception(
            "glossary: upsert_terms_for_user failed (best-effort) sub=%s",
            user_sub,
        )
