#!/usr/bin/env bash
# Commit + push la branche courante vers GitHub, puis build container
# sur la VM cloud (bandwidth interne SCW).
#
# Usage :
#   deploy/scripts/commit-push-build.sh [--multi-arch] ["message de commit"]
#
# Par défaut : build linux/amd64 uniquement (~2x plus rapide, c'est ce qui
# tourne sur les nœuds Kapsule SCW). L'arm64 sert surtout pour Docker
# Desktop local sur Mac M1/M2/M3.
#
#   --multi-arch / --arm   ajoute linux/arm64 en plus (build complet)
#
# Si des modifs sont uncommitées et qu'aucun message n'est passé en argv,
# le script demande le message en interactif.
#
# Variables d'env optionnelles :
#   REMOTE_HOST   cible SSH (par défaut: root@198.51.100.10 = build-vm)
#   REMOTE_REPO   chemin du clone sur la VM (par défaut: /root/mirai-mesreunions)
#   PLATFORMS     override complet de la liste de plateformes buildx
#                 (ex: PLATFORMS=linux/arm64 pour ne builder QUE arm64)
#   SCW_SECRET_KEY  obligatoire en local, transmise au build via stdin (jamais argv)
#
# Le script partage son avancement étape par étape sur stdout/stderr.

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-root@198.51.100.10}"
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

[ -n "${SCW_SECRET_KEY:-}" ] || fail "SCW_SECRET_KEY non défini en local (export ou source la config)"
ok "SCW_SECRET_KEY présent en environnement local"

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
# La VM doit donc les recevoir hors-git pour que `kustomize build`
# fonctionne lors d'un éventuel apply depuis la VM.
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

# 4. Build container sur la VM cloud
step "Build container sur $REMOTE_HOST"
info "synchro repo + buildx ($BUILD_PLATFORMS) + push registry (bandwidth interne SCW)"
info "clé SCW transmise via stdin (jamais en argv ni en log)"

REMOTE_SCRIPT='set -e
cd "'"$REMOTE_REPO"'"
export PLATFORMS='"'$BUILD_PLATFORMS'"'

echo "  ▸ fetch origin '"$BRANCH"' (avec mise à jour explicite du tracking ref)"
git fetch --quiet origin "'"$BRANCH"':refs/remotes/origin/'"$BRANCH"'"

if git show-ref --verify --quiet "refs/heads/'"$BRANCH"'"; then
  git checkout --quiet "'"$BRANCH"'"
else
  git checkout --quiet -b "'"$BRANCH"'" "origin/'"$BRANCH"'"
fi
git reset --hard --quiet "origin/'"$BRANCH"'"
echo "  ▸ HEAD = $(git log --oneline -1)"

echo "  ▸ lecture SCW_SECRET_KEY depuis stdin"
IFS= read -r SCW_SECRET_KEY
export SCW_SECRET_KEY

echo "  ▸ prépare buildx (driver docker-container)"
# Le format réel de `buildx ls` met un astérisque sur le builder actif :
# "scw-multi*  docker-container ...". On match donc le nom seul.
if ! docker buildx inspect scw-multi >/dev/null 2>&1; then
  docker buildx create --name scw-multi --driver docker-container --bootstrap >/dev/null
fi
docker buildx use scw-multi

echo "  ▸ lance deploy/scripts/build-push-scw.sh"
bash deploy/scripts/build-push-scw.sh
'

printf '%s\n' "$SCW_SECRET_KEY" | ssh "$REMOTE_HOST" "$REMOTE_SCRIPT"
ok "image construite et poussée sur rg.fr-par.scw.cloud"

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
