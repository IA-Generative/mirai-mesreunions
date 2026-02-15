#!/usr/bin/env bash
set -euo pipefail

# Build and push image(s) to Scaleway Container Registry.
# Uses SCW_SECRET_KEY from local environment for auth.
#
# Usage:
#   SCW_SECRET_KEY=... ./deploy/scripts/build-push-scw.sh
#   SCW_SECRET_KEY=... ./deploy/scripts/build-push-scw.sh v1.2.3
#
# Optional env:
#   REGISTRY_HOST=rg.fr-par.scw.cloud
#   REGISTRY_NAMESPACE=scw-registry-namespace
#   IMAGE_NAME=secure-audio-upload
#   SCW_REGISTRY_USERNAME=nologin
#   PLATFORMS=linux/amd64,linux/arm64

TAG="${1:-}"
REGISTRY_HOST="${REGISTRY_HOST:-rg.fr-par.scw.cloud}"
REGISTRY_NAMESPACE="${REGISTRY_NAMESPACE:-scw-registry-namespace}"
IMAGE_NAME="${IMAGE_NAME:-secure-audio-upload}"
SCW_REGISTRY_USERNAME="${SCW_REGISTRY_USERNAME:-nologin}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

if [[ -z "${SCW_SECRET_KEY:-}" ]]; then
  echo "ERROR: SCW_SECRET_KEY is not set in environment."
  exit 1
fi

if [[ -z "${TAG}" ]]; then
  TAG="$(date +%Y%m%d-%H%M%S)"
fi

IMAGE_BASE="${REGISTRY_HOST}/${REGISTRY_NAMESPACE}/${IMAGE_NAME}"
IMAGE_TAGGED="${IMAGE_BASE}:${TAG}"
IMAGE_LATEST="${IMAGE_BASE}:latest"

echo "Logging in to ${REGISTRY_HOST}..."
echo "${SCW_SECRET_KEY}" | docker login "${REGISTRY_HOST}" -u "${SCW_REGISTRY_USERNAME}" --password-stdin >/dev/null

if docker buildx version >/dev/null 2>&1; then
  echo "Building and pushing multi-arch image with buildx..."
  docker buildx build \
    --platform "${PLATFORMS}" \
    -f deploy/docker/Dockerfile \
    -t "${IMAGE_TAGGED}" \
    -t "${IMAGE_LATEST}" \
    --push \
    .
else
  echo "buildx not found, using classic docker build/push..."
  docker build -f deploy/docker/Dockerfile -t "${IMAGE_TAGGED}" -t "${IMAGE_LATEST}" .
  docker push "${IMAGE_TAGGED}"
  docker push "${IMAGE_LATEST}"
fi

echo "Done."
echo "Pushed:"
echo "  - ${IMAGE_TAGGED}"
echo "  - ${IMAGE_LATEST}"
