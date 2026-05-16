#!/usr/bin/env bash
# Verify that an expired/missing refresh token is handled cleanly:
#   transcription_status = mcr_auth_failed, no infinite retry, log line
#   stating the user must re-login.
#
# Usage:
#   INT_KUBECONFIG=… USER_SUB=… ./tests/scenarios/mcr_auth_failure.sh

set -uo pipefail

INT_KUBECONFIG="${INT_KUBECONFIG:?Set INT_KUBECONFIG}"
USER_SUB="${USER_SUB:?Set USER_SUB}"

int_() { kubectl --kubeconfig="$INT_KUBECONFIG" "$@"; }

pass() { printf "\033[32mPASS\033[0m  %s\n" "$1"; }
fail() { printf "\033[31mFAIL\033[0m  %s\n  → %s\n" "$1" "$2"; }

echo "1. Removing the stored refresh token for $USER_SUB"
int_ -n audio-internal exec deploy/postgres-internal -- \
  psql -U audio_int -d audio_upload_int -c \
  "DELETE FROM oidc_refresh_tokens WHERE user_sub='$USER_SUB';"

echo
echo "2. Now upload an audio file from the mobile flow under this user_sub."
read -p "Press Enter once the file is at status 'transcoded' on the admin dashboard..."

echo
echo "3. Waiting up to 90s for the internal-ingester to mark mcr_auth_failed..."
for i in $(seq 1 18); do
  STATUS=$(int_ -n audio-internal exec deploy/postgres-internal -- \
    psql -U audio_int -d audio_upload_int -tAc \
    "SELECT transcription_status FROM user_audio_files WHERE user_sub='$USER_SUB' ORDER BY created_at DESC LIMIT 1;" 2>/dev/null)
  if [[ "$STATUS" == *mcr_auth_failed* ]]; then
    pass "transcription_status flipped to mcr_auth_failed as expected"
    break
  fi
  sleep 5
done
[[ "$STATUS" == *mcr_auth_failed* ]] || { fail "timeout — status did not flip" "got $STATUS"; exit 1; }

echo
echo "4. Verify internal-ingester logs explicitly mention re-login requirement:"
int_ -n audio-internal logs -l app=internal-ingester --tail=200 \
  | grep -E "no refresh token stored|re-login" | tail -3 || \
  fail "no log line about missing refresh token" "check logs manually"

echo
echo "Done."
