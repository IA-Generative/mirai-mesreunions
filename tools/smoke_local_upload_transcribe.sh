#!/usr/bin/env bash
# smoke_local_upload_transcribe.sh — détecte le bug « uploads locaux jamais
# transcrits » (incident 2026-06-11 #2, Fix B commit 07131c5).
#
# Cause racine : un upload local (interface web, code L-…) ne crée AUCUN jeton,
# donc process_file_ready (file-mover / dmz-to-internal-bridge) posait
# auto_transcribe=False en dur → transcription_status='disabled' à vie. Fix B :
# quand il n'y a pas de jeton, on applique resolve_auto_transcribe(False), qui
# suit AUTO_TRANSCRIBE_POLICY. Il faut donc CETTE policy posée à 'on'|'user' sur
# les 3 émetteurs — DONT le file-mover (zone externe), oublié de l'incident #1.
#
# Ce détecteur combine : (1) policy présente sur les émetteurs, (2) absence
# d'uploads LOCAUX bloqués en 'disabled'. Exit != 0 si régression.
#
# Usage :
#   NS=audio-internal \
#   EXT_CONTEXT=<ctx external-gw> EXT_NS=audio-external \
#   ./tools/smoke_local_upload_transcribe.sh
#   (contexte kubectl courant = interne ; EXT_CONTEXT requis pour le file-mover)
set -euo pipefail

NS="${NS:-audio-internal}"
DB_SECRET="${DB_SECRET:-internal-db-secret}"
PG_POD="${PG_POD:-postgres-internal-0}"
WINDOW_HOURS="${WINDOW_HOURS:-48}"
INT_ISSUERS="${INT_ISSUERS:-device-token-authority mesreunions-web}"
EXT_CONTEXT="${EXT_CONTEXT:-}"          # contexte kubectl du cluster external-gw
EXT_NS="${EXT_NS:-audio-external}"
EXT_ISSUER="${EXT_ISSUER:-dmz-to-internal-bridge}"

fail=0

pol_ok() { # $1 = valeur policy ; ok si 'on' ou 'user'
  [[ "$1" == "on" || "$1" == "user" ]]
}

echo "== 1) AUTO_TRANSCRIBE_POLICY sur les émetteurs internes =="
for d in $INT_ISSUERS; do
  pol="$(kubectl -n "$NS" set env "deploy/$d" --list 2>/dev/null \
        | grep -E '^AUTO_TRANSCRIBE_POLICY=' | cut -d= -f2- || true)"
  if pol_ok "${pol:-}"; then echo "  ✓ $d : AUTO_TRANSCRIBE_POLICY=$pol"
  else echo "  ✗ $d : AUTO_TRANSCRIBE_POLICY='${pol:-<ABSENT→off>}'"; fail=1; fi
done

echo
echo "== 2) AUTO_TRANSCRIBE_POLICY sur le file-mover (zone externe, Fix B) =="
if [[ -z "$EXT_CONTEXT" ]]; then
  echo "  ! EXT_CONTEXT non fourni — check file-mover SKIP (export EXT_CONTEXT=<ctx external-gw>)"
  echo "    NB : sans policy sur $EXT_ISSUER, TOUT upload local retombe en 'disabled'."
else
  pol="$(kubectl --context="$EXT_CONTEXT" -n "$EXT_NS" set env "deploy/$EXT_ISSUER" --list 2>/dev/null \
        | grep -E '^AUTO_TRANSCRIBE_POLICY=' | cut -d= -f2- || true)"
  if pol_ok "${pol:-}"; then echo "  ✓ $EXT_ISSUER : AUTO_TRANSCRIBE_POLICY=$pol"
  else echo "  ✗ $EXT_ISSUER : AUTO_TRANSCRIBE_POLICY='${pol:-<ABSENT→off>}' (uploads locaux jamais transcrits)"; fail=1; fi
fi

echo
echo "== 3) Uploads LOCAUX (code L-) bloqués en 'disabled' (fenêtre ${WINDOW_HOURS}h) =="
PW="$(kubectl -n "$NS" get secret "$DB_SECRET" -o jsonpath='{.data.INT_DB_PASSWORD}' | base64 -d)"
DB="$(kubectl -n "$NS" get secret "$DB_SECRET" -o jsonpath='{.data.INT_DB_NAME}' | base64 -d)"
USR="$(kubectl -n "$NS" get secret "$DB_SECRET" -o jsonpath='{.data.INT_DB_USER}' | base64 -d)"
q() { kubectl -n "$NS" exec "$PG_POD" -- env PGPASSWORD="$PW" \
        psql -U "$USR" -d "$DB" -tAc "$1" 2>/dev/null; }

disabled="$(q "SELECT count(*) FROM user_audio_files
              WHERE transcription_status::text='disabled'
                AND original_session_code LIKE 'L-%'
                AND created_at > now() - interval '${WINDOW_HOURS} hours';")"
disabled="${disabled//[[:space:]]/}"
echo "  uploads locaux 'disabled' récents : ${disabled:-?}"
if [[ "${disabled:-0}" -gt 0 ]]; then
  echo "  ✗ ${disabled} upload(s) local(aux) jamais transcrit(s)."
  q "SELECT '    - '||user_email||'  '||original_session_code||'  '||created_at::timestamp(0)
       FROM user_audio_files
      WHERE transcription_status::text='disabled' AND original_session_code LIKE 'L-%'
        AND created_at > now() - interval '${WINDOW_HOURS} hours'
      ORDER BY created_at DESC LIMIT 20;"
  fail=1
else
  echo "  ✓ aucun upload local bloqué sur la fenêtre"
fi

echo
if [[ "$fail" -ne 0 ]]; then
  echo "RÉSULTAT : ✗ TRANSCRIPTION UPLOADS LOCAUX KO — voir mémoire project_local_uploads_no_token_disabled"
  exit 1
fi
echo "RÉSULTAT : ✓ uploads locaux transcrits (policy + 0 disabled)"
