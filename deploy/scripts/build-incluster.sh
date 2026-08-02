#!/usr/bin/env bash
# Build in-cluster de l'image monorepo avec BuildKit rootless.
#
# Remplace le build sur VM (ssh + docker buildx sur openrag-01-et) : le build
# tourne dans un Job Kubernetes sur le cluster Scaleway, et le push part du
# cluster — pas de démon Docker local, pas de VM à maintenir, pas de
# SCW_SECRET_KEY qui transite par un poste (cf. ADR-0004).
#
# Même moteur que buildx (BuildKit), donc mêmes images : la bascule ne fait
# pas diverger le résultat. Kaniko a été écarté (projet archivé depuis 2025).
#
#   deploy/scripts/build-incluster.sh                    # tag horodaté, branche courante
#   deploy/scripts/build-incluster.sh v1.2.3             # tag explicite
#   deploy/scripts/build-incluster.sh --cache            # cache de couches au registre
#   deploy/scripts/build-incluster.sh --dry-run          # affiche le Job, n'applique rien
#
# Options :
#   -n, --namespace <ns>     namespace du Job     (défaut: $BUILD_NAMESPACE / audio-internal)
#   -c, --kube-context <ctx> contexte kubectl     (défaut: $BUILD_KUBE_CONTEXT / contexte courant)
#       --ref <git-ref>      réf à construire     (défaut: branche courante)
#       --push-secret <nom>  secret de push       (défaut: $BUILD_PUSH_SECRET / scw-registry-push)
#       --platforms <liste>  plateformes cibles   (défaut: linux/amd64 — voir MULTI-ARCH)
#       --cache              cache de couches partagé au registre (<image>-cache)
#       --no-latest          ne pousse pas le tag `latest`
#       --allow-unpushed     désactive la garde « commit local présent sur origin »
#       --keep               conserve le Job après succès (debug)
#       --dry-run            rend le manifeste sur stdout sans l'appliquer
#
# ATTENTION — le build part de GIT, pas de votre copie de travail : ce qui est
# construit est ce qui est POUSSÉ sur origin. La garde ci-dessous refuse de
# lancer si le HEAD local n'est pas sur origin (contournable avec
# --allow-unpushed, par exemple pour reconstruire une réf ancienne).
#
# MULTI-ARCH — construire une architecture étrangère aux nœuds exige QEMU/binfmt
# enregistré sur l'hôte (DaemonSet tonistiigi/binfmt). Les nœuds Kapsule sont en
# amd64, ce qui est la seule cible utile en prod-bêta : le défaut est donc
# linux/amd64. L'arm64 (Docker Desktop sur Mac) reste le domaine du build local.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE="$ROOT_DIR/deploy/kubernetes/buildkit/buildkit-build-job.yaml"
BUILDKIT_IMAGE="${BUILDKIT_IMAGE:-moby/buildkit:v0.31.2-rootless}"
DOCKERFILE="deploy/docker/Dockerfile"

TAG=""; NAMESPACE=""; KUBE_CONTEXT=""; GIT_REF=""; PUSH_SECRET=""; PLATFORMS=""
USE_CACHE=0; PUSH_LATEST=1; ALLOW_UNPUSHED=0; KEEP=0; DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--namespace)     NAMESPACE="$2"; shift 2 ;;
    -c|--kube-context)  KUBE_CONTEXT="$2"; shift 2 ;;
    --ref)              GIT_REF="$2"; shift 2 ;;
    --push-secret)      PUSH_SECRET="$2"; shift 2 ;;
    --platforms)        PLATFORMS="$2"; shift 2 ;;
    --cache)            USE_CACHE=1; shift ;;
    --no-latest)        PUSH_LATEST=0; shift ;;
    --allow-unpushed)   ALLOW_UNPUSHED=1; shift ;;
    --keep)             KEEP=1; shift ;;
    --dry-run)          DRY_RUN=1; shift ;;
    -h|--help)          sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)                 echo "ERROR: option inconnue: $1" >&2; exit 2 ;;
    *)                  [ -z "$TAG" ] && TAG="$1" || { echo "ERROR: tag déjà fourni ($TAG)" >&2; exit 2; }; shift ;;
  esac
done

# ---- Registre : mêmes variables que build-push-scw.sh, mêmes défauts.
# REGISTRY_NAMESPACE n'a PAS de valeur par défaut utilisable : le placeholder
# historique `scw-registry-namespace` produisait un push silencieusement cassé.
REGISTRY_HOST="${REGISTRY_HOST:-rg.fr-par.scw.cloud}"
REGISTRY_NAMESPACE="${REGISTRY_NAMESPACE:-}"
IMAGE_NAME="${IMAGE_NAME:-secure-audio-upload}"
if [ -z "$REGISTRY_NAMESPACE" ] || [ "$REGISTRY_NAMESPACE" = "scw-registry-namespace" ]; then
  echo "ERROR: REGISTRY_NAMESPACE non défini (ou laissé au placeholder)." >&2
  echo "       export REGISTRY_NAMESPACE=<namespace registre SCW> avant de lancer." >&2
  exit 1
fi
IMAGE="$REGISTRY_HOST/$REGISTRY_NAMESPACE/$IMAGE_NAME"

NAMESPACE="${NAMESPACE:-${BUILD_NAMESPACE:-audio-internal}}"
KUBE_CONTEXT="${KUBE_CONTEXT:-${BUILD_KUBE_CONTEXT:-}}"
PUSH_SECRET="${PUSH_SECRET:-${BUILD_PUSH_SECRET:-scw-registry-push}}"
PLATFORMS="${PLATFORMS:-linux/amd64}"

# Tag par défaut : même convention horodatée que build-push-scw.sh.
[ -n "$TAG" ] || TAG="$(date +%Y%m%d-%H%M%S)"

# ---- Réf git à construire + garde « c'est bien ce commit qui sera construit »
GIT_URL="$(git -C "$ROOT_DIR" remote get-url origin)"
case "$GIT_URL" in *.git) ;; *) GIT_URL="$GIT_URL.git" ;; esac
BRANCH="${GIT_REF:-$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)}"
LOCAL_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"
REMOTE_SHA="$(git -C "$ROOT_DIR" ls-remote origin "$BRANCH" 2>/dev/null | awk '{print $1}' | head -1)"

if [ "$ALLOW_UNPUSHED" -eq 0 ]; then
  if [ -z "$REMOTE_SHA" ]; then
    echo "ERROR: '$BRANCH' n'existe pas sur origin — le build part de git." >&2
    echo "       Poussez la branche, ou relancez avec --allow-unpushed." >&2
    exit 1
  fi
  if [ "$REMOTE_SHA" != "$LOCAL_SHA" ]; then
    echo "ERROR: le HEAD local ($(git -C "$ROOT_DIR" rev-parse --short HEAD)) diffère de" >&2
    echo "       origin/$BRANCH (${REMOTE_SHA:0:7}). Le cluster construirait le commit DISTANT." >&2
    echo "       Poussez d'abord, ou relancez avec --allow-unpushed." >&2
    exit 1
  fi
  if [ -n "$(git -C "$ROOT_DIR" status --porcelain -- ':!docs' 2>/dev/null)" ]; then
    echo "WARN: copie de travail modifiée — ces changements ne seront PAS dans l'image." >&2
  fi
fi
BUILD_SHA="${REMOTE_SHA:-$LOCAL_SHA}"

# Contexte git du frontend dockerfile.v0 : dépôt public, aucun credential.
GIT_CONTEXT="$GIT_URL#$BRANCH"

# ---- Destinations et arguments optionnels
DESTINATIONS="$IMAGE:$TAG"
[ "$PUSH_LATEST" -eq 1 ] && DESTINATIONS="$DESTINATIONS,$IMAGE:latest"

EXTRA_ARGS=()
if [ "$USE_CACHE" -eq 1 ]; then
  EXTRA_ARGS+=("--export-cache=type=registry,ref=$IMAGE-cache,mode=max"
               "--import-cache=type=registry,ref=$IMAGE-cache")
fi

JOB_NAME="mcr-build-${TAG//[^a-zA-Z0-9]/-}-${BUILD_SHA:0:7}"
JOB_NAME="$(echo "$JOB_NAME" | tr '[:upper:]' '[:lower:]' | cut -c1-63)"

MANIFEST="$(
  TEMPLATE="$TEMPLATE" JOB_NAME="$JOB_NAME" NAMESPACE="$NAMESPACE" \
  BUILDKIT_IMAGE="$BUILDKIT_IMAGE" GIT_CONTEXT="$GIT_CONTEXT" DOCKERFILE="$DOCKERFILE" \
  DESTINATIONS="$DESTINATIONS" TAG="$TAG" GIT_SHA="$BUILD_SHA" PUSH_SECRET="$PUSH_SECRET" \
  PLATFORMS="$PLATFORMS" \
  EXTRA_ARGS="$(printf '%s\n' "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")" \
  python3 - <<'PY'
import os
tpl = open(os.environ["TEMPLATE"], encoding="utf-8").read()
extra = [a for a in os.environ.get("EXTRA_ARGS", "").splitlines() if a.strip()]
tokens = {
    "__JOB_NAME__": os.environ["JOB_NAME"],
    "__NAMESPACE__": os.environ["NAMESPACE"],
    "__BUILDKIT_IMAGE__": os.environ["BUILDKIT_IMAGE"],
    "__GIT_CONTEXT__": os.environ["GIT_CONTEXT"],
    "__DOCKERFILE__": os.environ["DOCKERFILE"],
    "__DESTINATIONS__": os.environ["DESTINATIONS"],
    "__PLATFORMS__": os.environ["PLATFORMS"],
    "__TAG__": os.environ["TAG"],
    "__GIT_SHA__": os.environ["GIT_SHA"],
    "__PUSH_SECRET__": os.environ["PUSH_SECRET"],
}
for k, v in tokens.items():
    tpl = tpl.replace(k, v)

# La ligne porteuse de __EXTRA_ARGS__ est remplacée par 0..n entrées, à son
# indentation propre : le gabarit reste ainsi du YAML valide et relisible.
out = []
for line in tpl.splitlines():
    if "__EXTRA_ARGS__" in line:
        indent = " " * (len(line) - len(line.lstrip()))
        out.extend(f'{indent}- "{a}"' for a in extra)
    else:
        out.append(line)
print("\n".join(out))
PY
)"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "$MANIFEST"
  exit 0
fi

KCTL=(kubectl)
[ -n "$KUBE_CONTEXT" ] && KCTL+=(--context "$KUBE_CONTEXT")

# `kubectl --context X config current-context` IGNORE --context : il lit le
# champ current-context du kubeconfig. Afficher ça donnait un cluster faux
# quand -c était passé — un opérateur pouvait croire que le build partait
# ailleurs. On affiche donc l'override quand il existe.
echo "== Build in-cluster (BuildKit rootless) =="
echo "   cluster     : ${KUBE_CONTEXT:-$(kubectl config current-context)}"
echo "   namespace   : $NAMESPACE"
echo "   source      : $GIT_CONTEXT @ ${BUILD_SHA:0:7}"
echo "   image       : $DESTINATIONS"
echo "   plateformes : $PLATFORMS"
echo "   secret      : $PUSH_SECRET"

if ! "${KCTL[@]}" get secret "$PUSH_SECRET" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "ERROR: secret '$PUSH_SECRET' absent du namespace '$NAMESPACE'." >&2
  echo "       Créez-le : deploy/scripts/create-registry-push-secret.sh" >&2
  exit 1
fi

"${KCTL[@]}" delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found >/dev/null
echo "$MANIFEST" | "${KCTL[@]}" apply -f - >/dev/null
echo "   job         : $JOB_NAME"

echo "-- logs --"
for _ in $(seq 1 60); do
  POD="$("${KCTL[@]}" get pods -n "$NAMESPACE" -l job-name="$JOB_NAME" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [ -n "$POD" ] && break
  sleep 2
done
[ -n "${POD:-}" ] || { echo "ERROR: aucun pod pour le job $JOB_NAME" >&2; exit 1; }

# Attendre que le conteneur ait démarré AVANT d'attacher : `logs -f` sur un pod
# en ContainerCreating échoue immédiatement, et on perdrait toute la sortie du
# build.
for _ in $(seq 1 150); do
  PHASE="$("${KCTL[@]}" get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  case "$PHASE" in Running|Succeeded|Failed) break ;; esac
  sleep 2
done
if ! "${KCTL[@]}" logs -f "$POD" -n "$NAMESPACE" 2>&1; then
  # Repli : le pod a pu terminer avant l'attache — on relit les logs figés.
  "${KCTL[@]}" logs "$POD" -n "$NAMESPACE" 2>&1 || echo "(logs indisponibles)"
fi

if "${KCTL[@]}" wait --for=condition=complete --timeout=45m \
     job/"$JOB_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "== OK : $DESTINATIONS poussé depuis le cluster (commit ${BUILD_SHA:0:7}) =="
  [ "$KEEP" -eq 1 ] || "${KCTL[@]}" delete job "$JOB_NAME" -n "$NAMESPACE" >/dev/null
  exit 0
fi

echo "== ÉCHEC du build. Job conservé pour diagnostic : $JOB_NAME ==" >&2
"${KCTL[@]}" describe job "$JOB_NAME" -n "$NAMESPACE" | tail -20 >&2
exit 1
