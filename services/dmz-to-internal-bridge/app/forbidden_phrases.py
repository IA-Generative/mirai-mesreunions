"""Pré-filtrage automatique des scories de transcription Whisper.

Charge la liste depuis ``transcription_forbidden_phrases`` (cf migration
021 + libs.shared.app.models.TranscriptionForbiddenPhrase) avec un cache
mémoire de 5 minutes pour ne pas hammer la DB à chaque transcription.

Filtrage appliqué dans ``puller._transcribe_via_kevent`` après Whisper
et AVANT diarization + étapes LLM downstream. Les segments Whisper dont
le ``text`` matche un pattern actif sont droppés ; on recalcule alors
``transcription_text`` (joint des segments restants) et ``words_flat``
(filtré par segment_id sortant) avant de continuer le pipeline.

Match : insensible à la casse + trim des espaces parasites en bord. Pas
de regex (volonté de garder la config admin compréhensible). Si plus
tard on veut du regex, ajouter un flag ``is_regex BOOL`` à la table.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Cache TTL : 5 minutes. Bon compromis entre fraîcheur (édit admin
# visible rapidement sans rollout) et charge DB. Configurable via env.
import os
_CACHE_TTL_S = float(os.environ.get("FORBIDDEN_PHRASES_CACHE_TTL_S", "300"))

# Cache global process-local. Liste ordonnée des phrases actives,
# normalisées (lower + trim) avec leur version originale (pour log).
# Format : [(phrase_norm, phrase_original), ...]
_cache_lock = threading.Lock()
_cache_phrases: list[tuple[str, str]] = []
_cache_loaded_at: float = 0.0


def _normalize(s: str) -> str:
    """Normalisation pour match : lower + strip ponctuation/espaces marginaux.

    Conservation conservative — on ne touche pas aux espaces internes ni
    aux apostrophes (qui font partie de la signature des phrases ciblées).
    """
    return (s or "").strip().lower()


def _load_from_db(session_factory) -> list[tuple[str, str]]:
    """Lit la liste active triée par ordering croissant (longs en premier)."""
    from libs.shared.app.models import TranscriptionForbiddenPhrase
    db = session_factory()
    try:
        rows = (
            db.query(TranscriptionForbiddenPhrase)
            .filter(TranscriptionForbiddenPhrase.is_active.is_(True))
            .order_by(TranscriptionForbiddenPhrase.ordering.asc())
            .all()
        )
        return [(_normalize(r.phrase), r.phrase) for r in rows]
    finally:
        db.close()


def get_forbidden_phrases(session_factory, *, force_refresh: bool = False
                           ) -> list[tuple[str, str]]:
    """Retourne la liste cachée des phrases interdites actives.

    Recharge depuis la DB si le cache a expiré (ou si ``force_refresh``).
    Process-local : chaque pod ingester a son propre cache. Acceptable
    parce que le TTL est court (5 min) et les édits admin sont rares.
    """
    global _cache_phrases, _cache_loaded_at
    now = time.monotonic()
    with _cache_lock:
        stale = (now - _cache_loaded_at) > _CACHE_TTL_S
        if force_refresh or stale or not _cache_phrases:
            try:
                _cache_phrases = _load_from_db(session_factory)
                _cache_loaded_at = now
                logger.info(
                    "forbidden_phrases: loaded %d active phrase(s) from DB",
                    len(_cache_phrases),
                )
            except Exception:
                logger.exception("forbidden_phrases: failed to load from DB")
                # Garder l'ancien cache si le rechargement échoue.
        return list(_cache_phrases)


def _segment_matches_any(seg_text: str, phrases_norm: list[tuple[str, str]]
                          ) -> Optional[str]:
    """Si le segment matche une des phrases interdites, renvoie l'original.

    Match : le texte du segment (normalisé) doit CONTENIR au moins une
    phrase interdite. Les phrases interdites typiques sont des hallucinations
    entières (le segment fait toute la phrase) ; mais Whisper peut aussi
    coller la phrase parasite à du contenu réel — dans ce cas on drop tout
    le segment (perte minime, sécurité maximale, alignée sur l'outil source).
    """
    norm = _normalize(seg_text)
    if not norm:
        return None
    for p_norm, p_orig in phrases_norm:
        if p_norm and p_norm in norm:
            return p_orig
    return None


def filter_whisper_segments(transcription: dict, session_factory) -> dict:
    """Filtre les segments d'une réponse Whisper verbose_json.

    Renvoie un nouveau dict avec :
    - ``segments`` filtré (segments matchant une phrase interdite retirés)
    - ``text`` recalculé depuis les segments restants
    - autres champs (``language``, ``duration``, ``task``) passés tels quels
    - ``forbidden_phrases_dropped`` : liste des matches (pour log/audit)

    Si la liste est vide ou la transcription n'a pas de segments, no-op.
    """
    segments = list(transcription.get("segments") or [])
    if not segments:
        return transcription
    phrases = get_forbidden_phrases(session_factory)
    if not phrases:
        return transcription

    kept: list[dict] = []
    dropped: list[dict] = []
    for seg in segments:
        seg_text = (seg.get("text") or "").strip()
        match = _segment_matches_any(seg_text, phrases)
        if match is not None:
            dropped.append({
                "matched_phrase": match,
                "segment_text": seg_text[:200],
                "start": seg.get("start"),
                "end": seg.get("end"),
            })
        else:
            kept.append(seg)

    if not dropped:
        return transcription  # rien à faire, transcription propre

    # Recompose le texte depuis les segments restants. Si rien ne reste
    # (cas extrême), garder le text original pour ne pas casser le contrat.
    new_text = "\n".join(
        (s.get("text") or "").strip()
        for s in kept
        if (s.get("text") or "").strip()
    ).strip()

    logger.info(
        "forbidden_phrases: dropped %d segment(s) / kept %d — examples: %s",
        len(dropped), len(kept),
        ", ".join(repr(d["matched_phrase"]) for d in dropped[:3]),
    )

    out = dict(transcription)
    out["segments"] = kept
    if new_text:
        out["text"] = new_text
    out["forbidden_phrases_dropped"] = dropped
    return out
