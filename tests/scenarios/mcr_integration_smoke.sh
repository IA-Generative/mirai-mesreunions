#!/usr/bin/env bash
# Smoke test runbook for the MCR push integration.
# Manual end-to-end scenario, run after the prod-bêta switch:
#   1. apply oidc-refresh-token-encryption secret (Fernet key)
#   2. apply migration 003
#   3. deploy mesreunions-web + admin-console with OIDC_OFFLINE_ACCESS=true
#   4. wait long enough for users to relog
#   5. set MCR_GATEWAY_URL on internal-ingester and flip MCR_PUSH_ENABLED=true
#   6. run this script

set -uo pipefail

INT_KUBECONFIG="${INT_KUBECONFIG:?Set INT_KUBECONFIG=path-to-internal-gw kubeconfig}"
EXT_KUBECONFIG="${EXT_KUBECONFIG:?Set EXT_KUBECONFIG=path-to-external-gw kubeconfig}"
USER_SUB="${USER_SUB:?Set USER_SUB=<the user sub used for the test login>}"

int_() { kubectl --kubeconfig="$INT_KUBECONFIG" "$@"; }

pass() { printf "\033[32mPASS\033[0m  %s\n" "$1"; }
fail() { printf "\033[31mFAIL\033[0m  %s\n  → %s\n" "$1" "$2"; }

# 1. Refresh token captured at login
echo
echo "1. Verifying that the refresh token was captured for $USER_SUB"
ROW=$(int_ -n audio-internal exec deploy/postgres-internal -- \
  psql -U audio_int -d audio_upload_int -tAc \
  "SELECT user_sub, last_login_at FROM oidc_refresh_tokens WHERE user_sub='$USER_SUB';" 2>/dev/null)
if [[ -n "$ROW" ]]; then
  pass "oidc_refresh_tokens row present for $USER_SUB ($ROW)"
else
  fail "no row in oidc_refresh_tokens" "log into mydevices first then re-run"
  exit 1
fi

# 2. Upload a file via the mobile flow (manual)
cat <<'EOF'

2. Now upload a small audio file from the mobile flow (mydevices QR → upload).
   Wait until the file appears as 'transcoded' in the admin dashboard.
   Then press Enter to continue.
EOF
read -p ""

# 3. After ~1-2 minutes of pipeline time, the row in user_audio_files should
#    flip to mcr_pushed.
echo "3. Waiting for transcription_status to flip (up to 90s)..."
for i in $(seq 1 18); do
  STATUS=$(int_ -n audio-internal exec deploy/postgres-internal -- \
    psql -U audio_int -d audio_upload_int -tAc \
    "SELECT transcription_status, mcr_meeting_id FROM user_audio_files WHERE user_sub='$USER_SUB' ORDER BY created_at DESC LIMIT 1;" 2>/dev/null)
  if [[ "$STATUS" == *mcr_pushed* ]]; then
    pass "transcription_status flipped to mcr_pushed ($STATUS)"
    break
  fi
  if [[ "$STATUS" == *mcr_auth_failed* ]]; then
    fail "user marked mcr_auth_failed — the user must re-login on mydevices" "$STATUS"
    exit 2
  fi
  if [[ "$STATUS" == *mcr_rejected* ]]; then
    fail "MCR rejected the meeting payload" "$STATUS"
    exit 2
  fi
  sleep 5
done
[[ "$STATUS" == *mcr_pushed* ]] || { fail "timeout — never flipped to mcr_pushed" "$STATUS"; exit 3; }

# 4. Verify the meeting exists in MCR (out-of-band — this script can't check the
#    MCR dashboard, ops must do that manually)
cat <<'EOF'

4. MANUAL CHECK: open the MCR dashboard (or call MCR's GET /meetings/{id}) with the
   meeting_id printed above and confirm the audio file is being transcribed under
   the right user.
EOF
echo
echo "Done."
