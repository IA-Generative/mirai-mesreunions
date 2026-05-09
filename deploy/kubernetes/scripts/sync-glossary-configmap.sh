#!/usr/bin/env bash
# Sync the local `glossaire/` directory to the `kevent-glossary` ConfigMap
# in the audio-internal namespace, then restart file-puller so the worker
# reloads the glossary at boot.
#
# When the ConfigMap is absent, file-puller falls back to the glossary
# baked into the image at /app/glossaire (Dockerfile COPY).
#
# Usage:
#   KUBECONFIG=…/kubeconfig-internal-gw.yaml ./sync-glossary-configmap.sh
#
# Requirements: kubectl with cluster-admin on the target context.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
GLOSSARY_DIR="${ROOT}/glossaire"
NAMESPACE="${KEVENT_GLOSSARY_NAMESPACE:-audio-internal}"
CONFIGMAP_NAME="${KEVENT_GLOSSARY_CONFIGMAP:-kevent-glossary}"

if [[ ! -d "${GLOSSARY_DIR}" ]]; then
    echo "ERROR: glossary directory ${GLOSSARY_DIR} missing" >&2
    exit 1
fi

# Count terms (loose check — counts non-empty / non-comment lines)
n_files=$(find "${GLOSSARY_DIR}" -maxdepth 1 -type f \( -name '*.md' -o -name '*.txt' -o -name '*.json' \) | wc -l | tr -d ' ')
echo "Sync ${n_files} glossary file(s) from ${GLOSSARY_DIR} → ConfigMap ${NAMESPACE}/${CONFIGMAP_NAME}"

kubectl create configmap "${CONFIGMAP_NAME}" \
    --namespace "${NAMESPACE}" \
    --from-file="${GLOSSARY_DIR}" \
    --dry-run=client -o yaml \
    | kubectl apply -f -

echo "Rollout restart file-puller so the worker reloads the glossary…"
kubectl --namespace "${NAMESPACE}" rollout restart deployment/file-puller
kubectl --namespace "${NAMESPACE}" rollout status deployment/file-puller --timeout=120s

echo "Done. Verify with:"
echo "  kubectl -n ${NAMESPACE} logs deploy/file-puller | grep -i 'Loaded.*glossary terms'"
