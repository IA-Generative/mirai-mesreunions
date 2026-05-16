"""Module glossary — utilitaires d'upsert dans user_glossary_terms.

Refacto PR3 : extrait du legacy bloc dans main.py, exposé comme fonction
publique consommée par les modules preparations + meetings via leur
service.py respectif.
"""

from .service import upsert_terms_for_user, extract_terms_from_brief

__all__ = ["upsert_terms_for_user", "extract_terms_from_brief"]
