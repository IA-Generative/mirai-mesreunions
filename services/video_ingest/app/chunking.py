"""Agrégation des segments fins (sous-titre par sous-titre) en chunks
temporels 60-90s avec chevauchement 15s (Principe 8).

But : permettre les liens horodatés (`?t=<start_seconds>s`) et limiter
les hallucinations futures du RAG (V2).

Pur, sans I/O. La sortie est sérialisable JSON (cf. `segments_json` de
`video_transcripts`).
"""

from __future__ import annotations

from typing import Iterable, TypedDict

from .types import TranscriptSegment


class ChunkDict(TypedDict):
    start_seconds: float
    end_seconds: float
    text: str


def chunk(
    segments: Iterable[TranscriptSegment],
    *,
    target_seconds: float = 75.0,    # milieu de la fenêtre 60-90s
    overlap_seconds: float = 15.0,
) -> list[ChunkDict]:
    """Agrège des segments fins en chunks temporels.

    Algorithme :
      - On accumule les segments dans le chunk courant tant qu'on n'a pas
        atteint `target_seconds` de durée écoulée depuis le début du chunk.
      - Au flush, on démarre le chunk suivant à `current_end - overlap`,
        en réincluant les segments qui chevauchent cette borne.

    Garanties :
      - Chunks triés par `start_seconds` croissants.
      - Tout segment apparaît dans au moins un chunk (les segments à
        cheval sur la frontière apparaissent dans deux chunks → overlap).
      - Aucun chunk vide en sortie.
    """
    segs = sorted(segments, key=lambda s: s.start_seconds)
    if not segs:
        return []

    chunks: list[ChunkDict] = []
    i = 0
    n = len(segs)

    while i < n:
        chunk_start = segs[i].start_seconds
        chunk_end = chunk_start
        chunk_segs: list[TranscriptSegment] = []
        j = i

        while j < n:
            s_end = segs[j].start_seconds + segs[j].duration_seconds
            if chunk_segs and (s_end - chunk_start) > target_seconds:
                # On a déjà au moins un segment et on dépasserait la cible
                # → on ferme ce chunk (ce segment ira au suivant ou
                # apparaîtra à cheval).
                break
            chunk_segs.append(segs[j])
            chunk_end = s_end
            j += 1

        chunks.append({
            "start_seconds": chunk_start,
            "end_seconds": chunk_end,
            "text": " ".join(s.text.strip() for s in chunk_segs if s.text.strip()),
        })

        # Tous les segments consommés → on a fini, pas de chunk supplémentaire
        # même si le chevauchement théorique pourrait en générer un.
        if j >= n:
            break

        # Borne de redémarrage : recul de `overlap_seconds` depuis la fin
        # actuelle. On cherche le premier segment qui démarre >= cette
        # borne, ce qui crée naturellement le chevauchement.
        next_start_threshold = chunk_end - overlap_seconds
        next_i = j  # fallback : pas de chevauchement si overlap > durée chunk
        for k in range(i + 1, j):
            if segs[k].start_seconds >= next_start_threshold:
                next_i = k
                break

        # Sécurité anti-boucle infinie : on doit toujours avancer.
        if next_i <= i:
            next_i = i + 1
        i = next_i

    return chunks
