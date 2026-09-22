"""Répare l'alignement des transcriptions importées depuis une source externe
à sous-titres (YouTube), en re-dérivant ``speaker_tagged_text`` et
``transcription_words_json`` depuis les chunks d'origine.

Pourquoi. Trois défauts du chemin de matérialisation ont produit, sur les
fiches déjà en base :
  - des lignes de continuation sans ``>`` (le ``\\n`` interne des cues
    survivait jusqu'au Markdown), qu'aucun parseur ne sait rattacher à un bloc
    — donc du texte présent en base mais JAMAIS affiché ;
  - des blocs dupliqués, le recouvrement de 15 s entre chunks n'étant pas
    réellement dédupliqué ;
  - une timeline non monotone, qui invalide la recherche dichotomique du
    surlignage karaoké côté frontend.

Le correctif de code règle les nouveaux imports ; ce script règle l'existant.
La source de vérité est ``video_transcripts.segments_json`` (les chunks tels
que produits par video-ingest), relue puis repassée dans les fonctions
corrigées de ``app/external_source.py``.

Ne touche NI ``transcription_text``, NI le compte-rendu, NI aucune colonne
dérivée d'un LLM. Idempotent : rejouable sans effet de bord.

Usage. ``tools/`` n'est PAS copié dans l'image (cf. deploy/docker/Dockerfile) :
il faut pousser le script dans le pod avant de l'exécuter ::

    CTX=<contexte internal-gw>
    POD=$(kubectl --context $CTX -n audio-internal get pod \\
            -l app=internal-ingester -o jsonpath='{.items[0].metadata.name}')
    kubectl --context $CTX -n audio-internal exec -i $POD -- \\
        sh -c 'mkdir -p /app/tools && cat > /app/tools/backfill_external_transcript_alignment.py' \\
        < tools/backfill_external_transcript_alignment.py
    kubectl --context $CTX -n audio-internal exec $POD -- \\
        python /app/tools/backfill_external_transcript_alignment.py

Dry-run par défaut. Ajouter ``--apply`` pour persister. Penser à ``rm`` le
script du pod ensuite (il disparaît de toute façon au prochain rollout).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import sessionmaker

from libs.shared.app.config import load_int_db
from libs.shared.app.models import UserAudioFile

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_external_alignment")


def _load_external_source():
    """``app.external_source`` in-pod, chemin de fichier en dev shell."""
    try:
        from app import external_source            # type: ignore
        return external_source
    except ImportError:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "services", "dmz-to-internal-bridge", "app", "external_source.py",
        )
        spec = importlib.util.spec_from_file_location("external_source", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


es = _load_external_source()

_HEAD_RE = re.compile(
    r"^\*\*([^*]+)\*\*\s*_\((\d+):(\d+(?:\.\d+)?)\s*→\s*(\d+):(\d+(?:\.\d+)?)\)_"
)

# Nombre de blocs voisins dans lesquels chercher une répétition. Un
# recouvrement de 15 s porte au plus une poignée de phrases.
_NEAR_DUP_WINDOW = 10


def _audit(speaker_tagged: str | None, words_json: str | None) -> dict:
    """Les trois symptômes, mesurés sur une paire de colonnes."""
    lines = (speaker_tagged or "").split("\n")
    quoted = [ln for ln in lines if ln.startswith(">")]
    orphans = [ln for ln in lines
               if ln.strip() and not ln.startswith(">") and not _HEAD_RE.match(ln)]
    try:
        words = json.loads(words_json) if words_json else []
    except (ValueError, TypeError):
        words = []
    backward = sum(1 for a, b in zip(words, words[1:])
                   if float(b.get("s", 0)) < float(a.get("s", 0)))
    # Doublons de RECOUVREMENT seulement : une répétition proche. Un même
    # bloc peut légitimement revenir plus loin dans une vidéo (« Merci. »,
    # « Très bien. ») — le compter comme un défaut donnerait un rapport faux.
    near_dups = sum(1 for i, ln in enumerate(quoted)
                    if ln in quoted[max(0, i - _NEAR_DUP_WINDOW):i])
    return {
        "blocks": len(quoted),
        "orphan_lines": len(orphans),
        "orphan_chars": sum(len(ln) for ln in orphans),
        "dup_blocks": near_dups,
        "words": len(words),
        "backward": backward,
        "shown_chars": sum(len(re.sub(r"^>\s?", "", ln).strip()) for ln in quoted),
    }


def _fmt(a: dict) -> str:
    return (f"blocs={a['blocks']} orphelines={a['orphan_lines']}"
            f"({a['orphan_chars']}c) doublons={a['dup_blocks']} "
            f"mots={a['words']} reculs={a['backward']} affiché={a['shown_chars']}c")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Persiste les changements (sinon dry-run).")
    parser.add_argument("--file-id", default=None,
                        help="Ne traiter qu'un user_audio_files.id.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap nombre de fiches traitées (debug).")
    parser.add_argument("--force-hidden-blocks", action="store_true",
                        help="Traiter aussi les fiches ayant des blocs barrés, "
                             "en RÉINITIALISANT hidden_block_indices (voir plus bas).")
    args = parser.parse_args()

    eng = create_engine(load_int_db().sync_url)
    db = sessionmaker(bind=eng)()

    try:
        sql = """
            SELECT u.id::text            AS uaf_id,
                   u.source_type::text   AS source_type,
                   u.speaker_tagged_text AS st,
                   u.transcription_words_json AS wj,
                   u.hidden_block_indices AS hidden,
                   vt.segments_json::text AS segments
              FROM user_audio_files u
              JOIN video_transcripts vt
                ON vt.video_source_id = u.external_video_source_id
             WHERE u.source_type::text = 'youtube_subtitle'
               AND u.external_video_source_id IS NOT NULL
               AND u.speaker_tagged_text IS NOT NULL
        """
        params = {}
        if args.file_id:
            sql += " AND u.id::text = :fid"
            params["fid"] = args.file_id
        sql += " ORDER BY u.created_at DESC"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"

        rows = db.execute(text(sql), params).mappings().all()
        logger.info("Fiches candidates : %d", len(rows))

        planned, skipped = [], 0
        for r in rows:
            before = _audit(r["st"], r["wj"])

            hidden = r["hidden"] or []
            if hidden and not args.force_hidden_blocks:
                # hidden_block_indices désigne les blocs par POSITION. La dédup
                # en retire (le recouvrement en crée ~10 %), donc les indices
                # barrés glisseraient sur d'autres blocs. On refuse plutôt que
                # de déplacer silencieusement les choix de l'utilisateur.
                logger.warning("  %s SAUTÉE — %d bloc(s) barré(s) ; "
                               "--force-hidden-blocks pour traiter en les "
                               "réinitialisant", r["uaf_id"][:8], len(hidden))
                skipped += 1
                continue

            try:
                chunks = json.loads(r["segments"] or "[]")
            except (ValueError, TypeError):
                logger.warning("  %s SAUTÉE — segments_json illisible", r["uaf_id"][:8])
                skipped += 1
                continue
            if not chunks:
                logger.warning("  %s SAUTÉE — aucun chunk source", r["uaf_id"][:8])
                skipped += 1
                continue

            sentences = es.sentence_align_segments(chunks)
            new_st = es.format_speaker_tagged_from_sentences(
                sentences, speaker_label="Intervenant_01")
            new_wj = json.dumps(
                es.flatten_segments_to_synthetic_words(sentences), ensure_ascii=False)
            after = _audit(new_st, new_wj)

            if new_st == (r["st"] or "") and new_wj == (r["wj"] or ""):
                logger.info("  %s déjà à jour", r["uaf_id"][:8])
                continue

            logger.info("  %s", r["uaf_id"][:8])
            logger.info("      avant : %s", _fmt(before))
            logger.info("      après : %s", _fmt(after))
            if after["orphan_lines"] or after["backward"] or after["dup_blocks"]:
                logger.warning("      ⚠ symptômes résiduels — à inspecter")
            planned.append((r["uaf_id"], new_st, new_wj, bool(hidden)))

        logger.info("À mettre à jour : %d | sautées : %d", len(planned), skipped)
        if not args.apply:
            logger.info("DRY-RUN — rien persisté. Relancer avec --apply.")
            return 0

        for uaf_id, new_st, new_wj, had_hidden in planned:
            values = {"speaker_tagged_text": new_st,
                      "transcription_words_json": new_wj}
            if had_hidden:
                # Cohérent avec --force-hidden-blocks : les positions ne
                # veulent plus rien dire après re-découpage.
                values["hidden_block_indices"] = []
            db.execute(update(UserAudioFile)
                       .where(UserAudioFile.id == uaf_id)
                       .values(**values))
        db.commit()
        logger.info("APPLIED : %d fiche(s) mise(s) à jour.", len(planned))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
