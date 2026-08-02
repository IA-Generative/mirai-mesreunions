#!/usr/bin/env bash
# Commit + push la branche courante vers GitHub, puis build container
# IN-CLUSTER (Job BuildKit rootless sur le cluster Scaleway).
#
# Usage :
#   deploy/scripts/commit-push-build.sh [--multi-arch] ["message de commit"]
#
# Depuis ADR-0004, le build ne passe plus par la VM cloud : il tourne dans un
# Job Kubernetes et le push part du cluster. Plus de SSH, plus de démon Docker
# à maintenir, et SCW_SECRET_KEY ne transite plus par un poste à chaque build
# (elle vit dans le secret de push du namespace). Le moteur est inchangé —
# BuildKit est déjà celui de `docker buildx`, donc les images ne divergent pas.
#
# Par défaut : build linux/amd64 uniquement, la seule architecture des nœuds
# Kapsule SCW. L'arm64 (Docker Desktop sur Mac M1/M2/M3) exige QEMU/binfmt sur
# les nœuds — non installé : utiliser un build local pour ce cas.
#
#   --multi-arch / --arm   ajoute linux/arm64 (nécessite binfmt sur les nœuds)
#
# Si des modifs sont uncommitées et qu'aucun message n'est passé en argv,
# le script demande le message en interactif.
#
# Variables d'env :
#   REGISTRY_NAMESPACE  obligatoire — namespace du registre SCW
#   BUILD_KUBE_CONTEXT  contexte kubectl du cluster de build (défaut: courant)
#   BUILD_NAMESPACE     namespace du Job de build (défaut: audio-internal)
#   BUILD_PUSH_SECRET   secret dockerconfigjson de push (défaut: scw-registry-push)
#   PLATFORMS           override de la liste de plateformes
#   REMOTE_HOST         OPTIONNEL — si défini, les zones gitignored sont encore
#                       rsyncées vers cette VM (utile si vous appliquez les
#                       overlays kustomize depuis elle). Sinon l'étape est sautée.
#
# Le script partage son avancement étape par étape sur stdout/stderr.

set -euo pipefail

# La VM n'est plus requise : elle ne sert qu'au sync optionnel des zones
# gitignored, pour qui applique les overlays kustomize depuis elle.
REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_REPO="${REMOTE_REPO:-/root/mirai-mesreunions}"

# Plateformes par défaut : amd64 seulement.
BUILD_PLATFORMS="${PLATFORMS:-linux/amd64}"

# Parse flags + premier arg positionnel = message de commit.
COMMIT_MSG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --multi-arch|--multi|--arm|--arm64)
      BUILD_PLATFORMS="linux/amd64,linux/arm64"
      shift
      ;;
    --platforms=*)
      BUILD_PLATFORMS="${1#--platforms=}"
      shift
      ;;
    --platforms)
      BUILD_PLATFORMS="${2:?--platforms attend une valeur}"
      shift 2
      ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    --)
      shift
      [ $# -gt 0 ] && COMMIT_MSG="$1" && shift
      ;;
    *)
      # premier arg restant = message de commit
      [ -z "$COMMIT_MSG" ] && COMMIT_MSG="$1" || true
      shift
      ;;
  esac
done

if [ -t 1 ]; then
  C_BOLD='\033[1m'; C_BLUE='\033[34m'; C_GREEN='\033[32m'
  C_YELLOW='\033[33m'; C_RED='\033[31m'; C_DIM='\033[2m'; C_RST='\033[0m'
else
  C_BOLD=''; C_BLUE=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_DIM=''; C_RST=''
fi

step() { printf "\n${C_BOLD}${C_BLUE}▶ %s${C_RST}\n" "$*"; }
ok()   { printf "  ${C_GREEN}✓${C_RST} %s\n" "$*"; }
info() { printf "  ${C_DIM}•${C_RST} %s\n" "$*"; }
warn() { printf "  ${C_YELLOW}!${C_RST} %s\n" "$*" >&2; }
fail() { printf "  ${C_RED}✗${C_RST} %s\n" "$*" >&2; exit 1; }

# 1. Préchecks
step "Préchecks"
git rev-parse --git-dir >/dev/null 2>&1 || fail "pas dans un git repo"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "HEAD" ] && fail "HEAD détaché — checkout une branche d'abord"
ok "branche courante = ${C_BOLD}$BRANCH${C_RST}"
ok "plateformes build = ${C_BOLD}$BUILD_PLATFORMS${C_RST}"

[ -n "${REGISTRY_NAMESPACE:-}" ] || fail "REGISTRY_NAMESPACE non défini (namespace du registre SCW)"
ok "REGISTRY_NAMESPACE présent"
command -v kubectl >/dev/null 2>&1 || fail "kubectl introuvable — requis pour le build in-cluster"
ok "kubectl disponible"

git ls-remote --exit-code origin >/dev/null 2>&1 || fail "origin injoignable"
ok "remote origin atteignable"

# 2. Commit si nécessaire
step "Commit"
if [ -n "$(git status --porcelain)" ]; then
  CHANGED="$(git status --porcelain | wc -l | tr -d ' ')"
  info "$CHANGED fichier(s) modifié(s) ou non suivi(s) :"
  git status --short | sed 's/^/      /'
  if [ -z "$COMMIT_MSG" ]; then
    # Tentative d'ouverture /dev/tty pour le mode interactif ; si pas de
    # TTY (background, CI, pipe), bascule sur l'erreur explicite.
    if [ -t 0 ] && [ -r /dev/tty ] && [ -w /dev/tty ]; then
      printf "  ${C_BOLD}message de commit ?${C_RST} "
      IFS= read -r COMMIT_MSG </dev/tty
      [ -n "$COMMIT_MSG" ] || fail "message vide, abandon"
    else
      fail "modifs non commitées et pas de message fourni — usage: $0 \"<message>\""
    fi
  fi
  git add -A
  git commit -m "$COMMIT_MSG"
  ok "commit créé : $(git log --oneline -1)"
else
  ok "working tree propre, rien à commit"
fi

# 3. Push
step "Push origin/$BRANCH"
git push -u origin "$BRANCH"
ok "push terminé — HEAD = $(git rev-parse --short HEAD)"

# 3-bis. Sync des bases gitignored (deploy/kubernetes/internal-zone et
# external-zone). Ces dossiers ne sont plus dans le repo public (cf
# commit ca0e41f) mais restent référencés par les overlays kustomize.
# Le build n'en a PAS besoin (le Dockerfile ne copie que du suivi-git) : cette
# étape ne sert qu'à qui applique les overlays depuis la VM. D'où le skip
# propre quand REMOTE_HOST n'est pas défini.
if [ -n "$REMOTE_HOST" ]; then
  step "Sync zones gitignored vers $REMOTE_HOST"
  ZONE_PATHS=(
    "deploy/kubernetes/internal-zone"
    "deploy/kubernetes/external-zone"
  )
  for p in "${ZONE_PATHS[@]}"; do
    if [ -d "$p" ]; then
      info "  rsync $p → $REMOTE_HOST:$REMOTE_REPO/$p"
      rsync -a --delete "$p/" "$REMOTE_HOST:$REMOTE_REPO/$p/"
    else
      info "  $p absent en local — skip"
    fi
  done
  ok "sync zones terminé"
else
  step "Sync zones gitignored"
  info "REMOTE_HOST non défini — skip (le build in-cluster n'en a pas besoin)"
fi

# 4. Build container in-cluster (Job BuildKit rootless)
step "Build container in-cluster"
info "Job BuildKit sur le cluster ($BUILD_PLATFORMS) — le push part du cluster"
info "source du build = origin/$BRANCH, pas la copie de travail"

PLATFORMS="$BUILD_PLATFORMS" bash "$(dirname "$0")/build-incluster.sh" --ref "$BRANCH"
ok "image construite et poussée sur ${REGISTRY_HOST:-rg.fr-par.scw.cloud}"

# 5. Pointeurs rollout
step "Terminé"
info "tags publiés : ${C_BOLD}:latest${C_RST} et ${C_BOLD}:YYYYMMDD-HHMMSS${C_RST}"
info "pour rollouter, choisis le service :"
echo "    # zone DMZ (external) :"
echo "    KUBECONFIG=deploy/kubernetes/kubeconfigs/kubeconfig-external-gw.yaml \\"
echo "      kubectl -n audio-external rollout restart deployment/mobile-upload-pwa"
echo "    # zone interne :"
echo "    KUBECONFIG=deploy/kubernetes/kubeconfigs/kubeconfig-internal-gw.yaml \\"
echo "      kubectl -n audio-internal rollout restart deployment/internal-ingester"
