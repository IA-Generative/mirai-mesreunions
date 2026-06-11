#!/usr/bin/env bash
# smoke_audio_retention.sh — vérifie la rétention des audios internes
# (demande 2026-06-11 : garder les audios 30 jours).
#
# INTERNAL_PURGE_MAX_AGE_DAYS gouverne la purge zone interne : run_internal_purge_once
# supprime l'ENREGISTREMENT COMPLET (audio S3 + transcription + CR) des UserAudioFile
# plus vieux que N jours. Défaut CODE = 7 jours (trop court). Ce détecteur garantit
# que la valeur déployée est >= la rétention attendue (sinon les audios sont purgés
# trop tôt). Lit la valeur RÉELLE vue par le process (printenv, capte aussi envFrom).
#
# Anomalie connue (séparée) : la purge tourne mais ne supprime rien actuellement
# (cf investigation 2026-06-11). Ce script signale aussi les UAF au-delà de la
# rétention (purge en retard / inactive) à titre informatif.
#
# Usage : NS=audio-internal EXPECTED_DAYS=30 ./tools/smoke_audio_retention.sh
set -euo pipefail

NS="${NS:-audio-internal}"
APP="${APP:-internal-ingester}"
EXPECTED_DAYS="${EXPECTED_DAYS:-30}"
DB_SECRET="${DB_SECRET:-internal-db-secret}"
PG_POD="${PG_POD:-postgres-internal-0}"

fail=0
POD="$(kubectl -n "$NS" get pods -l app="$APP" -o jsonpath='{.items[0].metadata.name}')"
[[ -n "$POD" ]] || { echo "✗ aucun pod $APP dans $NS"; exit 2; }

echo "== 1) INTERNAL_PURGE_MAX_AGE_DAYS effectif (pod $POD) =="
val="$(kubectl -n "$NS" exec "$POD" -- sh -c 'printf "%s" "${INTERNAL_PURGE_MAX_AGE_DAYS:-}"' 2>/dev/null || true)"
val="${val//[[:space:]]/}"
if [[ -z "$val" ]]; then
  echo "  ✗ INTERNAL_PURGE_MAX_AGE_DAYS NON DÉFINI → défaut code 7j (< ${EXPECTED_DAYS}j attendus)"
  fail=1
elif [[ "$val" -lt "$EXPECTED_DAYS" ]]; then
  echo "  ✗ INTERNAL_PURGE_MAX_AGE_DAYS=${val}j < ${EXPECTED_DAYS}j attendus (purge trop agressive)"
  fail=1
else
  echo "  ✓ INTERNAL_PURGE_MAX_AGE_DAYS=${val}j (>= ${EXPECTED_DAYS}j)"
fi

echo
echo "== 2) Observabilité purge : UAF au-delà de la rétention =="
PW="$(kubectl -n "$NS" get secret "$DB_SECRET" -o jsonpath='{.data.INT_DB_PASSWORD}' | base64 -d)"
DB="$(kubectl -n "$NS" get secret "$DB_SECRET" -o jsonpath='{.data.INT_DB_NAME}' | base64 -d)"
USR="$(kubectl -n "$NS" get secret "$DB_SECRET" -o jsonpath='{.data.INT_DB_USER}' | base64 -d)"
days="${val:-$EXPECTED_DAYS}"
over="$(kubectl -n "$NS" exec "$PG_POD" -- env PGPASSWORD="$PW" \
        psql -U "$USR" -d "$DB" -tAc \
        "SELECT count(*) FROM user_audio_files WHERE created_at < now() - interval '${days} days';" 2>/dev/null)"
over="${over//[[:space:]]/}"
echo "  UAF plus vieux que la rétention (${days}j) : ${over:-?}"
if [[ "${over:-0}" -gt 0 ]]; then
  echo "  ! ${over} UAF au-delà de ${days}j encore présents — purge en retard ou inactive"
  echo "    (informatif : non bloquant pour la rétention ; cf bug purge à instrumenter)"
fi

echo
if [[ "$fail" -ne 0 ]]; then
  echo "RÉSULTAT : ✗ RÉTENTION AUDIO TROP COURTE (< ${EXPECTED_DAYS}j)"
  exit 1
fi
echo "RÉSULTAT : ✓ rétention audio >= ${EXPECTED_DAYS}j"
