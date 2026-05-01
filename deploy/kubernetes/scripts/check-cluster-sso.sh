#!/usr/bin/env bash
# Test depuis un cluster k8s si SSO Mirai (<sso-host>) est atteignable.
# Permet de déterminer si le cluster joue la zone externe (peut atteindre le SSO) ou
# la zone interne (égress restreint, ne peut pas).
#
# Usage:
#   ./check-cluster-sso.sh <chemin-kubeconfig>
#   ./check-cluster-sso.sh <chemin-kubeconfig> <hostname-cible>      # override hostname
#
# Exemple:
#   ./check-cluster-sso.sh deploy/kubernetes/kubeconfigs/<external-cluster>.kubeconfig

set -euo pipefail

KUBECONFIG_PATH="${1:-}"
TARGET_HOST="${2:-<sso-host>}"

if [[ -z "${KUBECONFIG_PATH}" ]]; then
  echo "Usage: $0 <kubeconfig-path> [target-hostname]" >&2
  echo "  default target-hostname: <sso-host>" >&2
  exit 2
fi

if [[ ! -f "${KUBECONFIG_PATH}" ]]; then
  echo "ERROR: kubeconfig file not found: ${KUBECONFIG_PATH}" >&2
  exit 2
fi

export KUBECONFIG="${KUBECONFIG_PATH}"

echo "=== Cluster context ==="
kubectl config current-context
kubectl cluster-info | head -3
echo ""

# Pod éphémère unique par run (évite collisions si lancé en parallèle)
POD_NAME="sso-egress-test-$(date +%s)-$RANDOM"
NAMESPACE="default"

echo "=== Egress test toward https://${TARGET_HOST}/ ==="
echo "Spawning ephemeral pod ${POD_NAME} in namespace ${NAMESPACE}..."
echo ""

# --restart=Never + --rm pour cleanup auto. curlimages/curl est minimal et signé.
# --connect-timeout 5 + --max-time 15 pour faire échouer rapidement si bloqué.
# -w pour exposer les codes / IP / verif TLS de manière parsable.
set +e
kubectl run "${POD_NAME}" \
  --namespace "${NAMESPACE}" \
  --image=curlimages/curl:8.7.1 \
  --restart=Never \
  --rm \
  --quiet \
  --attach \
  --command -- \
  curl -sS \
       --connect-timeout 5 \
       --max-time 15 \
       -o /dev/null \
       -w "HTTP code:        %{http_code}\nResolved IP:      %{remote_ip}\nTLS verify result: %{ssl_verify_result}\nDNS time (ms):    %{time_namelookup}\nConnect time (ms): %{time_connect}\nTotal time (ms):  %{time_total}\n" \
       "https://${TARGET_HOST}/"
RC=$?
set -e

echo ""
if [[ "${RC}" -eq 0 ]]; then
  echo "VERDICT: ce cluster ATTEINT ${TARGET_HOST}."
  echo "         → probablement la **zone externe (DMZ)** dans le pattern CDS."
else
  echo "VERDICT: ce cluster N'ATTEINT PAS ${TARGET_HOST} (curl exit ${RC})."
  echo "         → probablement la **zone interne** (égress restreint)."
  echo "         Causes possibles: DNS bloqué, NetworkPolicy/FQDN whitelist, pas d'IP routable."
fi

exit "${RC}"
