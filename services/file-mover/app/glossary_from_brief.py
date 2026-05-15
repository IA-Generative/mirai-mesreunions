"""Extraction de glossaires depuis un MeetingBrief.

Deux fonctions complémentaires (cf §5.1 du plan v2) :

- ``extract_whisper_initial_prompt(brief_json, documents)`` produit un mini
  glossaire ciblé pour le champ ``initial_prompt`` du modèle Whisper. Cap
  dur **50 termes ≈ 200 tokens** pour rester sous la limite 244 du modèle.
  Sortie : phrase naturelle, pas une liste sèche (meilleur biais lexical).
- ``extract_full_glossary_terms_from_brief(brief_json, documents)`` produit
  un glossaire complet (cap 200 termes) pour le glossary_correction LLM
  post-Whisper. Pas de contrainte de taille, mais cap pour ne pas dépasser
  KEVENT_GLOSSARY_MAX_TERMS_PER_CALL.

Heuristiques cumulatives :
  1. Sigles (regex ``\\b[A-Z]{2,}(?:\\d+)?\\b``) — les plus utiles à Whisper
  2. Noms propres des participants_notes[].name (déjà filtrés par le LLM)
  3. Noms propres des agenda[].title (regex
     ``\\b[A-Z][a-zéèêàâïô]{3,}\\b``)
  4. Termes répétés ≥ 2 fois dans documents[].text_extract
  5. Vocabulaire de l'agenda (key_questions[]) — uniquement pour le full

Les stopwords FR capitalisés en début de phrase sont filtrés.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Optional


# Stopwords FR fréquents qui apparaissent capitalisés en début de phrase ou
# de titre — à filtrer pour ne pas polluer le glossaire.
_STOPWORDS_FR = {
    "Le", "La", "Les", "Un", "Une", "Des", "De", "Du", "Au", "Aux",
    "Et", "Ou", "Mais", "Donc", "Or", "Ni", "Car",
    "Ce", "Cette", "Ces", "Cet",
    "Avec", "Sans", "Pour", "Par", "Sur", "Sous", "Dans", "Vers", "Chez",
    "Notre", "Votre", "Leur", "Mon", "Ton", "Son",
    "Plus", "Moins", "Très", "Trop", "Aussi",
    "Comment", "Pourquoi", "Quel", "Quelle", "Quels", "Quelles",
    "Sujet", "Objet", "Point", "Réunion", "Comité",
    "Si", "Selon", "Entre",
}

_SIGLE_RE = re.compile(r"\b[A-Z]{2,}(?:\d+)?\b")
_PROPER_RE = re.compile(r"\b[A-ZÉÈÊÀÂÏÔÛÇ][a-zéèêàâïôûç]{3,}\b")


def _text_fields(brief_json: dict) -> list[str]:
    """Concatène les champs textuels d'un brief_json pour la regex de sigles."""
    if not isinstance(brief_json, dict):
        return []
    parts: list[str] = []
    for key in ("subject", "objective_reformulated", "context_summary"):
        v = brief_json.get(key)
        if isinstance(v, str):
            parts.append(v)
    for key in ("participants_notes", "agenda", "open_threads", "risks"):
        items = brief_json.get(key) or []
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict):
                for k in ("name", "title", "summary", "source", "context", "role"):
                    v = it.get(k)
                    if isinstance(v, str):
                        parts.append(v)
                kqs = it.get("key_questions")
                if isinstance(kqs, list):
                    parts.extend(q for q in kqs if isinstance(q, str))
            elif isinstance(it, str):
                parts.append(it)
    return parts


def _extract_sigles(texts: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in texts:
        for m in _SIGLE_RE.findall(t or ""):
            if m not in seen:
                seen.add(m)
                out.append(m)
    return out


def _extract_proper_nouns(texts: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in texts:
        for m in _PROPER_RE.findall(t or ""):
            if m in _STOPWORDS_FR or m in seen:
                continue
            seen.add(m)
            out.append(m)
    return out


def _participant_names(brief_json: dict) -> list[str]:
    out: list[str] = []
    items = (brief_json or {}).get("participants_notes") or []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            n = it.get("name")
            if isinstance(n, str) and n.strip():
                # Découpe sur les espaces pour exposer chaque mot capitalisé
                # individuellement (cas "Jean Dupont").
                for tok in n.split():
                    cleaned = tok.strip(".,;:()[]")
                    if cleaned and cleaned[0].isupper() and cleaned not in _STOPWORDS_FR:
                        out.append(cleaned)
                # Pousse aussi la chaîne complète (utile pour Whisper qui
                # bénéficie du nom assemblé dans la phrase).
                out.append(n.strip())
    return out


def _agenda_titles(brief_json: dict) -> list[str]:
    out: list[str] = []
    for it in (brief_json or {}).get("agenda") or []:
        if isinstance(it, dict):
            t = it.get("title")
            if isinstance(t, str) and t.strip():
                out.append(t.strip())
    return out


def _document_repeated_terms(documents: Optional[list], min_count: int = 2,
                              min_length: int = 5) -> list[str]:
    """Termes apparaissant ≥ ``min_count`` fois dans documents[].text_extract."""
    if not documents:
        return []
    counter: Counter[str] = Counter()
    for d in documents:
        if not isinstance(d, dict):
            continue
        text = d.get("text_extract") or ""
        if not isinstance(text, str):
            continue
        # Recherche les sigles + noms propres (qui sont les seuls pertinents
        # pour un glossaire de transcription).
        for m in _SIGLE_RE.findall(text):
            counter[m] += 1
        for m in _PROPER_RE.findall(text):
            if m not in _STOPWORDS_FR:
                counter[m] += 1
    return [t for t, c in counter.most_common() if c >= min_count and len(t) >= min_length]


# ─── Whisper initial_prompt (cap 50 termes / 200 tokens) ─────────


def extract_whisper_initial_prompt(brief_json: dict,
                                    documents: Optional[list] = None,
                                    max_terms: int = 50) -> str:
    """Produit la phrase ``initial_prompt`` Whisper depuis un brief.

    Format de sortie : une phrase naturelle de type
    ``"Réunion entre Jean Dupont et Marie Bonnet sur le projet DTNUM,
    comité COPIL DGSI 2026 sur RGPD."`` — meilleur biais lexical pour
    Whisper qu'une liste sèche.
    """
    if not isinstance(brief_json, dict):
        return ""

    sigles = _extract_sigles(_text_fields(brief_json))[:30]
    participants = _participant_names(brief_json)[:10]
    agenda_titles = _agenda_titles(brief_json)[:5]

    # Combine en gardant la priorité : sigles > participants > agenda.
    seen: set[str] = set()
    selected: list[str] = []
    for src in (sigles, participants, agenda_titles):
        for t in src:
            if t and t not in seen:
                seen.add(t)
                selected.append(t)
                if len(selected) >= max_terms:
                    break
        if len(selected) >= max_terms:
            break

    if not selected:
        return ""

    # Construit une phrase naturelle.
    parts: list[str] = []
    persons = [t for t in selected if " " in t][:5]
    if persons:
        parts.append(f"Réunion entre {', '.join(persons[:-1])} et {persons[-1]}" if len(persons) > 1
                     else f"Réunion avec {persons[0]}")
    sigles_present = [t for t in selected if _SIGLE_RE.fullmatch(t)][:8]
    if sigles_present:
        parts.append(f"sur les sujets {', '.join(sigles_present)}")
    titles = [t for t in selected if t not in persons and not _SIGLE_RE.fullmatch(t)][:3]
    if titles:
        parts.append(f"abordant {', '.join(titles)}")

    sentence = ", ".join(parts).strip()
    if sentence and not sentence.endswith("."):
        sentence += "."
    return sentence


# ─── Glossaire complet pour glossary_correction LLM ──────────────


def extract_full_glossary_terms_from_brief(brief_json: dict,
                                            documents: Optional[list] = None,
                                            cap: int = 200) -> set[str]:
    """Extrait l'ensemble dédupliqué des termes glossaire d'un brief.

    Sortie capée à ``cap`` termes. Le ``filter_relevant()`` du
    glossary_loader filtrera encore selon la transcription effective.
    """
    if not isinstance(brief_json, dict):
        return set()

    texts = _text_fields(brief_json)
    out: list[str] = []
    seen: set[str] = set()

    def _push(items: Iterable[str]):
        for it in items:
            if not it:
                continue
            it = it.strip()
            if len(it) < 2 or it in seen or it in _STOPWORDS_FR:
                continue
            seen.add(it)
            out.append(it)
            if len(out) >= cap:
                return True
        return False

    if _push(_extract_sigles(texts)):
        return set(out)
    if _push(_participant_names(brief_json)):
        return set(out)
    if _push(_extract_proper_nouns(texts)):
        return set(out)
    if _push(_document_repeated_terms(documents)):
        return set(out)
    # Vocabulaire de l'agenda (key_questions et titres ≥ 5 chars).
    agenda_vocab: list[str] = []
    for it in (brief_json or {}).get("agenda") or []:
        if not isinstance(it, dict):
            continue
        for k in ("title",):
            v = it.get(k)
            if isinstance(v, str):
                agenda_vocab.extend(w for w in v.split() if len(w) >= 5)
        kqs = it.get("key_questions") or []
        if isinstance(kqs, list):
            for q in kqs:
                if isinstance(q, str):
                    agenda_vocab.extend(w for w in q.split() if len(w) >= 5)
    _push(agenda_vocab)
    return set(out)
