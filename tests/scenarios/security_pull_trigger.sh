#!/usr/bin/env bash
# Security smoke tests for the pull-trigger HTTP endpoint.
# Run AFTER the cross-cluster wake-up has been deployed and ingress IPs are
# whitelisted. These are interactive — they need real kubectl + aws/scw +
# the actual prod-bêta DNS/cert.
#
# Usage:
#   TRIGGER_URL=https://pull-trigger.fake-domain.name/api/v1/pull-trigger \
#   TOKEN=$(kubectl ... -o jsonpath='{.data.token}' | base64 -d) \
#   ./tests/scenarios/security_pull_trigger.sh
#
# Each test prints PASS/FAIL and the relevant response excerpt.

set -uo pipefail

TRIGGER_URL="${TRIGGER_URL:?Set TRIGGER_URL=https://pull-trigger.fake-domain.name/api/v1/pull-trigger}"
TOKEN="${TOKEN:?Set TOKEN=<INTERNAL_PUSH_TRIGGER_TOKEN value>}"

pass() { printf "\033[32mPASS\033[0m  %s\n" "$1"; }
fail() { printf "\033[31mFAIL\033[0m  %s\n  → %s\n" "$1" "$2"; }

# --- C1. Bearer rotation (manual) -----------------------------------------
echo
echo "C1. Bearer rotation flow (manual checklist):"
cat <<'EOF'
   1. kubectl -n audio-internal create secret generic internal-push-trigger-secret \
        --from-literal=token=$(openssl rand -hex 32) --dry-run=client -o yaml | kubectl apply -f -
   2. Same on audio-external.
   3. kubectl rollout restart deploy/internal-ingester && deploy/dmz-to-internal-bridge (both clusters).
   4. Old token must now return 401; new token must succeed.
EOF

# --- C2. Bearer enforcement -----------------------------------------------
echo
echo "C2. Bearer enforcement on /api/v1/pull-trigger"
http_no_auth=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$TRIGGER_URL")
[[ "$http_no_auth" == "401" ]] && pass "no Authorization header → 401 ($http_no_auth)" \
    || fail "expected 401 without bearer, got $http_no_auth"

http_bad=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer not-the-token" "$TRIGGER_URL")
[[ "$http_bad" == "401" ]] && pass "bad bearer → 401 ($http_bad)" \
    || fail "expected 401 with bad bearer, got $http_bad"

http_ok=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" "$TRIGGER_URL")
[[ "$http_ok" == "200" ]] && pass "good bearer → 200 ($http_ok)" \
    || fail "expected 200 with good bearer, got $http_ok"

# --- C3. nginx ACL (run from non-whitelisted IP) --------------------------
echo
echo "C3. ACL nginx — must be run from a NON-whitelisted IP (e.g. via VPN with"
echo "    different egress, or a pod in a non-whitelisted cluster)."
echo "    Expected: 403 (nginx HTML page, NOT a Flask JSON 401/200)."
echo "    To run: same curl as C2 but from outside the allowlist."

# --- C4. Payload oversize -------------------------------------------------
echo
echo "C4. Payload size limit (1m)"
http_413=$(dd if=/dev/zero bs=1M count=2 2>/dev/null | \
    curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" \
    --data-binary @- \
    "$TRIGGER_URL")
[[ "$http_413" == "413" ]] && pass "2 MiB payload → 413 ($http_413)" \
    || fail "expected 413 for oversize payload, got $http_413"

# --- C5. Replay attack (idempotence) --------------------------------------
echo
echo "C5. Replay attack — re-publish a previously-handled message 100× via"
echo "    rabbitmqadmin and verify only one UserAudioFile row is created."
cat <<'EOF'
   Manual steps:
   1. Pick a message known to have been processed (file_id present in DB).
   2. for i in $(seq 1 100); do
        kubectl -n audio-external exec rabbitmq-0 -- rabbitmqadmin \
          -u "$U" -p "$P" -V audio_pipeline publish \
          routing_key=internal_pull payload="$KNOWN_PAYLOAD"
      done
   3. Wait 90 s (2-3 polling ticks + room).
   4. SELECT count(*) FROM user_audio_files WHERE stored_filename='<known-key>';
      → must be exactly 1 (idempotence guard).
EOF

# --- C6. URL injection (config) -------------------------------------------
echo
echo "C6. URL injection via INTERNAL_PUSH_TRIGGER_URL"
echo "    Set INTERNAL_PUSH_TRIGGER_URL=file:///etc/passwd in the dmz-to-internal-bridge"
echo "    deployment, rolling-restart, then check logs for:"
echo "      'HTTP trigger DISABLED (INTERNAL_PUSH_TRIGGER_URL is empty or not a valid URL)'"
echo "    No requests.post must be issued. Reset the env var to the proper URL"
echo "    once verified."

echo
echo "Done."
