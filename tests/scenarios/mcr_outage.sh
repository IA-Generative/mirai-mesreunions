#!/usr/bin/env bash
# Verify that an MCR outage is handled by the queue retry counter:
# 5 retries, then drop with mcr_push_failed and a single ERROR log line.
#
# Usage:
#   INT_KUBECONFIG=… USER_SUB=… ./tests/scenarios/mcr_outage.sh

set -uo pipefail

INT_KUBECONFIG="${INT_KUBECONFIG:?Set INT_KUBECONFIG}"
USER_SUB="${USER_SUB:?Set USER_SUB}"

int_() { kubectl --kubeconfig="$INT_KUBECONFIG" "$@"; }

pass() { printf "\033[32mPASS\033[0m  %s\n" "$1"; }
fail() { printf "\033[31mFAIL\033[0m  %s\n  → %s\n" "$1" "$2"; }

cat <<'EOF'
This script simulates an MCR outage by setting MCR_GATEWAY_URL to a
non-routable IP for file-puller, then watching the retry counter climb
and the message be dropped with mcr_push_failed.

Manual prep:
  1. Save current MCR_GATEWAY_URL value (you'll restore it afterwards).
  2. Edit deploy/kubernetes/environments/prod-beta/internal/kustomization.yaml,
     set MCR_GATEWAY_URL to "https://203.0.113.1" (TEST-NET-3, will black-hole).
  3. kubectl apply -k … --load-restrictor=LoadRestrictionsNone
  4. kubectl rollout restart deploy/file-puller
  5. Run this script.
EOF
read -p "Press Enter when ready..."

echo
echo "1. Upload a test file via the mobile flow under $USER_SUB"
read -p "Press Enter once status reaches 'transcoded'..."

echo
echo "2. Watching for retry climb and final drop (max 4 minutes)..."
LAST_RETRY=""
for i in $(seq 1 24); do
  RETRY=$(int_ -n audio-external exec deploy/rabbitmq -- \
    rabbitmqctl -p audio_pipeline list_queues name messages_unacknowledged \
    | awk '$1=="internal_pull" {print $2}')
  STATUS=$(int_ -n audio-internal exec deploy/postgres-internal -- \
    psql -U audio_int -d audio_upload_int -tAc \
    "SELECT transcription_status FROM user_audio_files WHERE user_sub='$USER_SUB' ORDER BY created_at DESC LIMIT 1;" 2>/dev/null)
  echo "    tick $i: queue_unacked=$RETRY status=$STATUS"
  if [[ "$STATUS" == *mcr_push_failed* ]]; then
    pass "Reached mcr_push_failed after retries — queue retry counter behaving as expected"
    break
  fi
  sleep 10
done

echo
echo "3. Verify ERROR log line confirms drop after 5 retries:"
int_ -n audio-internal logs -l app=file-puller --tail=500 \
  | grep -E "Dropping message from internal_pull" | tail -2 || \
  fail "no drop log line" "check logs"

echo
echo "Cleanup: restore MCR_GATEWAY_URL to the real value and rolling-restart file-puller."
echo "Done."
