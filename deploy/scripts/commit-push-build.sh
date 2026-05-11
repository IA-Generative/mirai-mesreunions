#!/usr/bin/env bash
# Commit + push la branche courante vers GitHub, puis build container
# multi-arch sur la VM cloud (bandwidth interne SCW).
#
# Usage :
#   deploy/scripts/commit-push-build.sh ["message de commit"]
#
# Si des modifs sont uncommitées et qu'aucun message n'est passé en argv,
# le script demande le message en interactif.
#
# Variables d'env optionnelles :
#   REMOTE_HOST   cible SSH (par défaut: root@198.51.100.10 = build-vm)
#   REMOTE_REPO   chemin du clone sur la VM (par défaut: /root/mirai-mesreunions)
#   SCW_SECRET_KEY  obligatoire en local, transmise au build via stdin (jamais argv)
#
# Le script partage son avancement étape par étape sur stdout/stderr.

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-root@198.51.100.10}"
REMOTE_REPO="${REMOTE_REPO:-/root/mirai-mesreunions}"

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

COMMIT_MSG="${1:-}"

# 1. Préchecks
step "Préchecks"
git rev-parse --git-dir >/dev/null 2>&1 || fail "pas dans un git repo"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "HEAD" ] && fail "HEAD détaché — checkout une branche d'abord"
ok "branche courante = ${C_BOLD}$BRANCH${C_RST}"

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
    if [ -t 0 ] || [ -e /dev/tty ]; then
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

# 4. Build container sur la VM cloud
step "Build container sur $REMOTE_HOST"
info "synchro repo + buildx multi-arch + push registry (bandwidth interne SCW)"
info "clé SCW transmise via stdin (jamais en argv ni en log)"

REMOTE_SCRIPT='set -e
cd "'"$REMOTE_REPO"'"

echo "  ▸ fetch origin '"$BRANCH"'"
git fetch --quiet origin "'"$BRANCH"'"

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
if ! docker buildx ls | grep -q "^scw-multi "; then
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
echo "      kubectl -n audio-external rollout restart deployment/upload-portal"
echo "    # zone interne :"
echo "    KUBECONFIG=deploy/kubernetes/kubeconfigs/kubeconfig-internal-gw.yaml \\"
echo "      kubectl -n audio-internal rollout restart deployment/file-puller"
