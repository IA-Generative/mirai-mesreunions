#!/usr/bin/env bash
# smoke_youtube_materialization.sh — détecte le bug « anciennes vidéos YouTube
# pas accessibles » (incident 2026-06-11 #3).
#
# Cause racine : un import YouTube crée une ligne `meetings` (video_source_id set)
# puis un « hook materialize » crée le user_audio_files (UAF) et relie le Meeting
# (meetings.user_audio_file_id). Quand le hook ne s'exécute pas (imports
# antérieurs au hook, env materialize manquante côté video-ingest, internal-ingester
# injoignable…), le Meeting reste en PLACEHOLDER ORPHELIN : video_source_id présent
# mais user_audio_file_id = NULL. La transcription existe pourtant dans
# video_transcripts → contenu présent mais clic en erreur.
#
# Ce détecteur compte les placeholders orphelins. Remédiation : POST
# /api/v1/external-source/materialize sur internal-ingester (idempotent).
# Cf docs + mémoire project_youtube_stale_placeholder_materialize.
#
# Usage : NS=audio-internal ./tools/smoke_youtube_materialization.sh
#   (contexte kubectl = cluster interne / internal-gw)
set -euo pipefail

NS="${NS:-audio-internal}"
DB_SECRET="${DB_SECRET:-internal-db-secret}"
PG_POD="${PG_POD:-postgres-internal-0}"
# Fenêtre de grâce : un import très récent peut être légitimement en cours de
# matérialisation. On ne compte « stale » qu'au-delà de ce seuil.
GRACE_MINUTES="${GRACE_MINUTES:-10}"

fail=0

PW="$(kubectl -n "$NS" get secret "$DB_SECRET" -o jsonpath='{.data.INT_DB_PASSWORD}' | base64 -d)"
DB="$(kubectl -n "$NS" get secret "$DB_SECRET" -o jsonpath='{.data.INT_DB_NAME}' | base64 -d)"
USR="$(kubectl -n "$NS" get secret "$DB_SECRET" -o jsonpath='{.data.INT_DB_USER}' | base64 -d)"
q() { kubectl -n "$NS" exec "$PG_POD" -- env PGPASSWORD="$PW" \
        psql -U "$USR" -d "$DB" -tAc "$1" 2>/dev/null; }

echo "== Réunions YouTube en placeholder orphelin (video_source_id sans UAF) =="
stale="$(q "SELECT count(*) FROM meetings
            WHERE video_source_id IS NOT NULL
              AND user_audio_file_id IS NULL
              AND trashed_at IS NULL
              AND created_at < now() - interval '${GRACE_MINUTES} minutes';")"
stale="${stale//[[:space:]]/}"
echo "  placeholders orphelins : ${stale:-?}"

if [[ "${stale:-0}" -gt 0 ]]; then
  echo "  ✗ ${stale} réunion(s) YouTube non matérialisée(s) — clic en erreur côté UI."
  echo "    détail (user / date / vidéo) :"
  q "SELECT '    - '||m.user_sub||'  '||m.created_at::timestamp(0)||'  '||coalesce(vs.title,vs.canonical_url,'?')
       FROM meetings m
       LEFT JOIN video_sources vs ON vs.id=m.video_source_id
      WHERE m.video_source_id IS NOT NULL AND m.user_audio_file_id IS NULL
        AND m.trashed_at IS NULL
        AND m.created_at < now() - interval '${GRACE_MINUTES} minutes'
      ORDER BY m.created_at DESC
      LIMIT 30;"
  echo "    → remédiation : materialize idempotent (cf mémoire)."
  fail=1
else
  echo "  ✓ aucune vidéo YouTube en attente de matérialisation"
fi

# Garde-fou complémentaire : un placeholder doit TOUJOURS avoir sa transcription
# côté video_transcripts (sinon ce n'est pas juste un défaut de matérialisation,
# le contenu lui-même manque → autre incident).
echo
echo "== Cohérence : placeholders sans transcription source =="
no_tx="$(q "SELECT count(*) FROM meetings m
            WHERE m.video_source_id IS NOT NULL AND m.user_audio_file_id IS NULL
              AND m.trashed_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM video_transcripts vt
                              WHERE vt.video_source_id = m.video_source_id);")"
no_tx="${no_tx//[[:space:]]/}"
if [[ "${no_tx:-0}" -gt 0 ]]; then
  echo "  ! ${no_tx} placeholder(s) SANS transcription source — pas un simple"
  echo "    défaut de matérialisation, vérifier le pipeline d'import video-ingest."
else
  echo "  ✓ tous les placeholders ont leur transcription source (matérialisables)"
fi

echo
if [[ "$fail" -ne 0 ]]; then
  echo "RÉSULTAT : ✗ VIDÉOS YOUTUBE NON MATÉRIALISÉES — voir mémoire project_youtube_stale_placeholder_materialize"
  exit 1
fi
echo "RÉSULTAT : ✓ matérialisation YouTube saine"
