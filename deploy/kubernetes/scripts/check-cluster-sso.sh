#!/usr/bin/env bash
# Test depuis un cluster k8s si un hôte SSO/OIDC cible est atteignable au niveau TLS.
# Permet de différencier la zone externe (qui doit pouvoir atteindre le SSO de la cible
# d'intégration) et la zone interne (égress restreint, ne doit pas).
#
# L'hôte cible n'a pas de valeur par défaut : il est lu dans la variable d'environnement
# CDS_TARGET_HOST ou passé en deuxième argument. Cela évite d'inscrire les hôtes
# d'intégration spécifiques dans un repo public.
#
# Usage:
#   CDS_TARGET_HOST=<sso-host> ./check-cluster-sso.sh <chemin-kubeconfig>
#   ./check-cluster-sso.sh <chemin-kubeconfig> <hostname-cible>
#
# Exemple:
#   CDS_TARGET_HOST=sso.example.org ./check-cluster-sso.sh \
#     deploy/kubernetes/kubeconfigs/<kubeconfig>.yaml

set -euo pipefail

KUBECONFIG_PATH="${1:-}"
TARGET_HOST="${2:-${CDS_TARGET_HOST:-}}"

if [[ -z "${KUBECONFIG_PATH}" ]]; then
  echo "Usage: $0 <kubeconfig-path> [target-hostname]" >&2
  echo "  alternatively, set CDS_TARGET_HOST env var" >&2
  exit 2
fi

if [[ -z "${TARGET_HOST}" ]]; then
  echo "ERROR: target hostname required" >&2
  echo "  pass as second argument or via CDS_TARGET_HOST env var" >&2
  echo "  (intentionally no default — target hosts live in *.local files)" >&2
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
