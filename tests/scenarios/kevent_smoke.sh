#!/usr/bin/env bash
# End-to-end smoke runbook for the Kevent transcription backend on prod-bêta.
# Activate the toggles progressively (5a → 5e in docs/integrate-with-kevent.md)
# and re-run this script after each step to confirm.
#
# Usage:
#   INT_KUBECONFIG=… USER_SUB=… ./tests/scenarios/kevent_smoke.sh

set -uo pipefail

INT_KUBECONFIG="${INT_KUBECONFIG:?Set INT_KUBECONFIG=path-to-internal-gw kubeconfig}"
USER_SUB="${USER_SUB:?Set USER_SUB=<the user sub used for the test login>}"

int_() { kubectl --kubeconfig="$INT_KUBECONFIG" "$@"; }

pass() { printf "\033[32mPASS\033[0m  %s\n" "$1"; }
fail() { printf "\033[31mFAIL\033[0m  %s\n  → %s\n" "$1" "$2"; }

cat <<'EOF'
This script assumes you have already :
  1. apply the migration 004
  2. provisioned the kevent-api-key K8s Secret in audio-internal
  3. set TRANSCRIPTION_BACKEND=kevent on the file-puller deployment
  4. uploaded a test audio file via the mobile flow
EOF
read -p "Press Enter once the file is at status 'transcoded' in the admin dashboard..."

echo
echo "=== 1. Wait for transcription_status to reach kevent_completed (or partially) ==="
for i in $(seq 1 60); do
  ROW=$(int_ -n audio-internal exec deploy/postgres-internal -- \
    psql -U audio_int -d audio_upload_int -tAc \
    "SELECT transcription_status, transcription_engine, transcription_language,
            (transcription_text IS NOT NULL),
            (diarization_json IS NOT NULL),
            (speaker_tagged_text IS NOT NULL),
            (cleaned_text IS NOT NULL),
            (reformulated_text IS NOT NULL),
            (meeting_analysis_json IS NOT NULL)
     FROM user_audio_files
     WHERE user_sub='$USER_SUB' ORDER BY created_at DESC LIMIT 1;" 2>/dev/null)
  if [[ "$ROW" == *kevent_completed* || "$ROW" == *kevent_partially_completed* ]]; then
    pass "row settled: $ROW"
    break
  fi
  sleep 5
done
[[ "$ROW" == *kevent_* ]] || { fail "no kevent status reached" "got $ROW"; exit 1; }

echo
echo "=== 2. Inspect each non-NULL output (sanity check) ==="
int_ -n audio-internal exec deploy/postgres-internal -- \
  psql -U audio_int -d audio_upload_int -c \
  "SELECT
     length(transcription_text)       AS transcription_chars,
     length(diarization_json)         AS diarization_chars,
     length(speaker_tagged_text)      AS speaker_tagged_chars,
     length(cleaned_text)             AS cleaned_chars,
     length(reformulated_text)        AS reformulated_chars,
     length(meeting_analysis_json)    AS analysis_chars
   FROM user_audio_files
   WHERE user_sub='$USER_SUB' ORDER BY created_at DESC LIMIT 1;"

echo
echo "=== 3. file-puller logs for this run ==="
int_ -n audio-internal logs -l app=file-puller --tail=200 \
  | grep -E "Kevent|speaker_names|oob_cleaning|reformulation|meeting_analysis|kevent_" \
  | tail -20

echo
echo "Done. For human quality review of LLM outputs see kevent_quality_check.md."
