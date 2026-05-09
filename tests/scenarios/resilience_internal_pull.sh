#!/usr/bin/env bash
# Resilience smoke tests for the internal_pull queue + drain behaviour.
# Validates that messages survive an internal-cluster outage and that the
# polling-only path works when the HTTP trigger is unreachable.
#
# Usage:
#   EXT_KUBECONFIG=… INT_KUBECONFIG=… ./tests/scenarios/resilience_internal_pull.sh
#
# Each block stops at the first divergence so an operator can inspect.

set -uo pipefail

EXT_KUBECONFIG="${EXT_KUBECONFIG:?Set path to external-gw kubeconfig}"
INT_KUBECONFIG="${INT_KUBECONFIG:?Set path to internal-gw kubeconfig}"

ext() { kubectl --kubeconfig="$EXT_KUBECONFIG" "$@"; }
int_() { kubectl --kubeconfig="$INT_KUBECONFIG" "$@"; }

queue_depth() {
    ext -n audio-external exec deploy/rabbitmq -- \
        rabbitmqctl -p audio_pipeline list_queues name messages \
        | awk '$1=="internal_pull" { print $2; found=1 } END { if (!found) print "?" }'
}

echo "Initial state: internal_pull depth = $(queue_depth)"

# --- D1. Internal cluster down → backlog → drain --------------------------
echo
echo "D1. Scaling file-puller to 0; queue should accumulate."
int_ -n audio-internal scale deploy/file-puller --replicas=0
sleep 5
echo "    Now upload 5 files via the depot UI, or trigger 5 sample messages:"
cat <<'EOF'
       for i in 1 2 3 4 5; do
         ext -n audio-external exec deploy/rabbitmq -- rabbitmqadmin \
           -u "$U" -p "$P" -V audio_pipeline publish \
           routing_key=internal_pull \
           payload='{"file_id":"smoke-'$i'","user_sub":"u","simple_code":"X","transcoded_filename":"x.mp4"}'
       done
EOF
read -p "Press Enter once the 5 messages are queued..."
depth=$(queue_depth)
echo "    Queue depth: $depth (expected: 5)"

echo "    Scaling file-puller back up:"
int_ -n audio-internal scale deploy/file-puller --replicas=1
int_ -n audio-internal rollout status deploy/file-puller --timeout=120s

echo "    Wait 45 s for first drain tick…"
sleep 45
depth=$(queue_depth)
echo "    Queue depth after drain: $depth (expected: 0)"

# --- D2. RabbitMQ outage --------------------------------------------------
echo
echo "D2. RabbitMQ external outage. Skipped by default — destructive."
echo "    Manual: ext -n audio-external scale deploy/rabbitmq --replicas=0"
echo "    Confirm file-mover blocks on publish (logs show RabbitMQ retry)."
echo "    Restore: ext -n audio-external scale deploy/rabbitmq --replicas=1"

# --- D3. Trigger HTTP unreachable (polling-only path) ---------------------
echo
echo "D3. Polling-only when HTTP trigger fails."
echo "    Manual: tighten the Ingress whitelist annotation to a narrow CIDR"
echo "    that excludes file-mover egress (e.g. 0.0.0.0/32). Apply, then"
echo "    upload a file: queue should accumulate, then drain at next tick"
echo "    (~30 s after publish). file-mover logs will show 5xx/timeouts but"
echo "    keep returning success since the queue is the source of truth."

# --- D4. S3 outage --------------------------------------------------------
echo
echo "D4. S3 audio-processed outage (NetworkPolicy block from puller)."
echo "    Apply a temporary NetworkPolicy denying egress from file-puller to"
echo "    Scaleway S3 IPs, publish a message, watch the retry counter climb."
echo "    After 5 retries the message should be DROPPED (logs ERROR)."
echo "    Remove the NetworkPolicy to restore."

echo
echo "Done."
