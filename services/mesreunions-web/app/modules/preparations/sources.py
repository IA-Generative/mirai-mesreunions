"""Contrat ``sources[]`` du wizard de préparation.

Le wizard n'alimentait le corpus que d'une façon : un dossier Drive dont
l'utilisateur collait l'URL (``drive_folder``). Ce module généralise à
plusieurs sources combinables, sans rien retirer :

  - ``drive_folder``  — un dossier Drive entier (le comportement historique)
  - ``drive_files``   — une sélection de fichiers dans un Drive
  - ``preparation``   — une réunion passée (brief, points clés, CR)
  - ``inline``        — un texte collé par l'utilisateur (mail, note)

Deux principes structurent le parsing :

1. **On refuse tôt et clairement.** Une entrée mal formée provoque un 400
   synchrone avec un message actionnable, plutôt qu'un worker asynchrone qui
   échouera avec un message technique que personne ne peut interpréter.

2. **Rien de ce qui vient du navigateur n'est de confiance.** Les textes
   ``inline`` sont assainis dès le parsing (et non au moment de l'assemblage),
   de sorte qu'un contenu hostile ne circule jamais dans le processus.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re

logger = logging.getLogger("mesreunions_web.preparations.sources")


def _sanitize_source_text(text: str) -> str:
    """Relaie vers l'assainisseur de ``meeting_prep``, chargé par chemin.

    Un ``from app.meeting_prep import …`` dépendrait de l'état de
    ``sys.modules``, que plusieurs suites de tests remplacent par des stubs.
    Le chargement par chemin absolu, déjà employé par ``meeting_prep`` pour
    ses propres dépendances, rend ce module autonome.
    """
    global _sanitize_impl
    if _sanitize_impl is None:
        path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "meeting_prep.py")
        )
        spec = importlib.util.spec_from_file_location("_prep_sources_sanitize", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _sanitize_impl = mod.sanitize_source_text
    return _sanitize_impl(text)


_sanitize_impl = None

# Types acceptés dans le payload.
SOURCE_TYPES = frozenset({"drive_folder", "drive_files", "preparation", "inline"})

# Sections d'une réunion passée qu'on peut verser au corpus.
PREPARATION_PARTS = frozenset({"brief", "key_points", "cr"})

# ─── Plafonds ─────────────────────────────────────────────────────
#
# Bornes dures, pensées en nombre d'appels réseau autant qu'en volume :
# chaque dossier coûte un listing, chaque fichier un téléchargement, chaque
# réunion deux à trois appels internes.
MAX_SOURCES = 20
MAX_DRIVE_FOLDERS = 5
MAX_DRIVE_FILES = 20
MAX_PREPARATIONS = 5
MAX_INLINE = 20
MAX_INLINE_CHARS = 20_000
MAX_INLINE_TOTAL_CHARS = 40_000
MAX_LABEL_CHARS = 200

# Un identifiant Drive ou de préparation est opaque et court. Cette forme
# rejette au passage les identifiants « chemin » (``../``) et les tentatives
# d'injection dans l'URL construite côté client Drive.
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class SourceError(ValueError):
    """Payload de sources invalide — porte un message destiné à l'utilisateur."""


def _clean_label(value, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    return value.strip()[:MAX_LABEL_CHARS] or fallback


def _require_id(value, what: str) -> str:
    raw = value if isinstance(value, str) else ""
    raw = raw.strip()
    if not raw or not _ID_RE.match(raw):
        raise SourceError(f"Identifiant {what} invalide.")
    return raw


def parse_sources(raw_sources, *, drive_folder_id=None, drive_folder_host=None) -> list[dict]:
    """Valide et normalise le tableau ``sources`` du payload.

    ``drive_folder_id`` (le champ historique, déjà extrait par l'appelant) est
    replacé **en tête** de la liste puis dédupliqué : un utilisateur qui a
    collé une URL *et* choisi des sources ne se retrouve pas avec son dossier
    ingéré deux fois.

    Lève ``SourceError`` avec un message lisible ; l'appelant le rend en 400.
    """
    sources: list[dict] = []

    if drive_folder_id:
        sources.append({
            "type": "drive_folder",
            "id": drive_folder_id,
            "host": drive_folder_host or None,
            "label": "",
        })

    if raw_sources is None:
        return sources
    if not isinstance(raw_sources, list):
        raise SourceError("Le champ sources doit être une liste.")
    if len(raw_sources) > MAX_SOURCES:
        raise SourceError(f"Trop de sources sélectionnées (maximum {MAX_SOURCES}).")

    counts = {"drive_folder": 0, "drive_files": 0, "preparation": 0, "inline": 0}
    inline_total = 0

    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise SourceError("Chaque source doit être un objet.")
        stype = (raw.get("type") or "").strip()
        if stype not in SOURCE_TYPES:
            raise SourceError(f"Type de source inconnu : {stype or '(vide)'}.")

        if stype == "drive_folder":
            counts["drive_folder"] += 1
            if counts["drive_folder"] > MAX_DRIVE_FOLDERS:
                raise SourceError(f"Trop de dossiers Drive (maximum {MAX_DRIVE_FOLDERS}).")
            sources.append({
                "type": "drive_folder",
                "id": _require_id(raw.get("id"), "de dossier Drive"),
                "drive": _clean_label(raw.get("drive")) or None,
                "host": _clean_label(raw.get("host")) or None,
                "label": _clean_label(raw.get("label")),
            })

        elif stype == "drive_files":
            items_raw = raw.get("items")
            if not isinstance(items_raw, list) or not items_raw:
                raise SourceError("Une source de fichiers Drive doit lister au moins un fichier.")
            items = []
            for entry in items_raw:
                if isinstance(entry, str):
                    entry = {"id": entry}
                if not isinstance(entry, dict):
                    raise SourceError("Chaque fichier Drive doit être un objet ou un identifiant.")
                items.append({
                    "id": _require_id(entry.get("id"), "de fichier Drive"),
                    "name": _clean_label(entry.get("name")),
                })
            counts["drive_files"] += len(items)
            if counts["drive_files"] > MAX_DRIVE_FILES:
                raise SourceError(f"Trop de fichiers Drive sélectionnés (maximum {MAX_DRIVE_FILES}).")
            sources.append({
                "type": "drive_files",
                "items": items,
                "drive": _clean_label(raw.get("drive")) or None,
                "host": _clean_label(raw.get("host")) or None,
            })

        elif stype == "preparation":
            counts["preparation"] += 1
            if counts["preparation"] > MAX_PREPARATIONS:
                raise SourceError(f"Trop de réunions précédentes (maximum {MAX_PREPARATIONS}).")
            include_raw = raw.get("include")
            include = [p for p in (include_raw or []) if p in PREPARATION_PARTS]
            if not include:
                # Défaut utile plutôt qu'un refus : l'utilisateur qui coche une
                # réunion sans préciser veut son contenu exploitable.
                include = ["brief", "key_points"]
            sources.append({
                "type": "preparation",
                "id": _require_id(raw.get("id"), "de préparation"),
                "include": include,
                "label": _clean_label(raw.get("label")),
            })

        else:  # inline
            counts["inline"] += 1
            if counts["inline"] > MAX_INLINE:
                raise SourceError(f"Trop de textes collés (maximum {MAX_INLINE}).")
            text = raw.get("text")
            if not isinstance(text, str) or not text.strip():
                raise SourceError("Un texte collé ne peut pas être vide.")
            # Assainissement dès le parsing : le contenu hostile ne circule pas.
            cleaned = _sanitize_source_text(text)
            if not cleaned:
                raise SourceError("Un texte collé ne peut pas être vide.")
            truncated = False
            if len(cleaned) > MAX_INLINE_CHARS:
                cleaned = cleaned[:MAX_INLINE_CHARS]
                truncated = True
            inline_total += len(cleaned)
            if inline_total > MAX_INLINE_TOTAL_CHARS:
                raise SourceError(
                    "Les textes collés dépassent au total "
                    f"{MAX_INLINE_TOTAL_CHARS} caractères."
                )
            sources.append({
                "type": "inline",
                "kind": _clean_label(raw.get("kind"), "note"),
                "title": _clean_label(raw.get("title"), "Texte collé"),
                "text": cleaned,
                "truncated": truncated,
            })

    return _dedupe(sources)


def _dedupe(sources: list[dict]) -> list[dict]:
    """Retire les doublons en conservant l'ordre (le premier gagne)."""
    seen: set = set()
    out: list[dict] = []
    for src in sources:
        key = _dedupe_key(src)
        if key in seen:
            continue
        seen.add(key)
        out.append(src)
    return out


def _dedupe_key(src: dict):
    stype = src["type"]
    if stype == "drive_folder":
        return (stype, src.get("drive"), src.get("host"), src["id"])
    if stype == "drive_files":
        return (stype, src.get("drive"), tuple(sorted(i["id"] for i in src["items"])))
    if stype == "preparation":
        return (stype, src["id"], tuple(sorted(src["include"])))
    # Deux textes identiques collés deux fois sont bien un doublon.
    return (stype, src["title"], hash(src["text"]))


def public_view(sources: list[dict]) -> list[dict]:
    """Vue traçable d'une sélection, **sans les contenus**.

    Rangée dans ``content._meta.sources`` pour que la fiche puisse dire d'où
    vient le brief. Le texte des sources ``inline`` en est délibérément exclu :
    c'est de la correspondance, elle n'a rien à faire dans un JSONB persisté
    ni dans un log.
    """
    out = []
    for src in sources:
        entry = {"type": src["type"]}
        if src["type"] == "drive_folder":
            entry["id"] = src["id"]
            if src.get("label"):
                entry["label"] = src["label"]
        elif src["type"] == "drive_files":
            entry["count"] = len(src["items"])
        elif src["type"] == "preparation":
            entry["id"] = src["id"]
            entry["include"] = src["include"]
            if src.get("label"):
                entry["label"] = src["label"]
        else:
            entry["title"] = src["title"]
            entry["chars"] = len(src["text"])
        out.append(entry)
    return out


def has_drive_source(sources: list[dict]) -> bool:
    """Un accès Drive est-il nécessaire pour cette génération ?

    Sert à ne pas exiger un jeton Drive d'un utilisateur qui ne nourrit son
    brief que de réunions passées et de textes collés.
    """
    return any(s["type"] in ("drive_folder", "drive_files") for s in sources)
