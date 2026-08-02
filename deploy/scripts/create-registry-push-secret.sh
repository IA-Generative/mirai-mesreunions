#!/usr/bin/env bash
# Crée (ou remplace) le secret de PUSH vers le registre Scaleway dans le
# namespace où tourne le Job de build in-cluster.
#
#   SCW_SECRET_KEY=... deploy/scripts/create-registry-push-secret.sh
#   SCW_SECRET_KEY=... deploy/scripts/create-registry-push-secret.sh -n audio-external
#
# Options :
#   -n, --namespace <ns>     namespace cible   (défaut: $BUILD_NAMESPACE / audio-internal)
#   -c, --kube-context <ctx> contexte kubectl  (défaut: $BUILD_KUBE_CONTEXT / contexte courant)
#       --name <nom>         nom du secret     (défaut: $BUILD_PUSH_SECRET / scw-registry-push)
#
# MOINDRE PRIVILÈGE — ce secret donne au cluster le droit d'ÉCRIRE dans le
# registre. Préférez une application IAM Scaleway dédiée, portant la seule
# permission `ContainerRegistryFullAccess` sur le projet, plutôt que de
# réutiliser une clé large déjà employée ailleurs (S3, Kapsule…). Le pod de
# build ne porte pas de jeton d'API Kubernetes, mais il porte CE secret :
# c'est le seul pouvoir qu'il faut lui accorder.
#
# La clé n'apparaît jamais en argv (visible dans `ps`) : `kubectl create
# secret` la reçoit via --docker-password sur un fichier temporaire à 0600.
set -euo pipefail

NAMESPACE=""; KUBE_CONTEXT=""; SECRET_NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    -n|--namespace)     NAMESPACE="$2"; shift 2 ;;
    -c|--kube-context)  KUBE_CONTEXT="$2"; shift 2 ;;
    --name)             SECRET_NAME="$2"; shift 2 ;;
    -h|--help)          sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                  echo "ERROR: argument inconnu: $1" >&2; exit 2 ;;
  esac
done

REGISTRY_HOST="${REGISTRY_HOST:-rg.fr-par.scw.cloud}"
REGISTRY_USERNAME="${SCW_REGISTRY_USERNAME:-nologin}"
NAMESPACE="${NAMESPACE:-${BUILD_NAMESPACE:-audio-internal}}"
KUBE_CONTEXT="${KUBE_CONTEXT:-${BUILD_KUBE_CONTEXT:-}}"
SECRET_NAME="${SECRET_NAME:-${BUILD_PUSH_SECRET:-scw-registry-push}}"

# Lecture de la clé : env d'abord, sinon saisie masquée. Le prompt évite deux
# pièges vus en vrai — la clé qui atterrit dans l'historique du shell, et le
# `SCW_SECRET_KEY=<clé valide>` recopié littéralement (zsh interprète alors
# `<clé` comme une redirection et le script part avec une clé vide).
if [ -z "${SCW_SECRET_KEY:-}" ]; then
  if [ -t 0 ] || [ -r /dev/tty ]; then
    printf "SCW_SECRET_KEY (saisie masquée) : " >&2
    IFS= read -rs SCW_SECRET_KEY </dev/tty
    printf "\n" >&2
  else
    echo "ERROR: SCW_SECRET_KEY non défini et pas de terminal pour la saisir." >&2
    exit 1
  fi
fi
[ -n "$SCW_SECRET_KEY" ] || { echo "ERROR: clé vide." >&2; exit 1; }

# Vérifie la clé AVANT de la sceller dans un secret : une clé périmée créerait
# un secret d'apparence saine qui ne casserait qu'au push, en fin de build.
echo "Vérification de la clé auprès de $REGISTRY_HOST…" >&2
HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' \
  -u "$REGISTRY_USERNAME:$SCW_SECRET_KEY" "https://$REGISTRY_HOST/v2/" || echo 000)"
if [ "$HTTP_CODE" != "200" ]; then
  echo "ERROR: le registre refuse la clé (HTTP $HTTP_CODE)." >&2
  echo "       Clé périmée ou héritée d'un environnement pollué (VSCode)." >&2
  echo "       Relancez depuis un terminal propre avec une clé valide." >&2
  exit 1
fi
echo "Clé acceptée par le registre (HTTP 200)." >&2

KCTL=(kubectl)
[ -n "$KUBE_CONTEXT" ] && KCTL+=(--context "$KUBE_CONTEXT")

# `kubectl --context X config current-context` IGNORE --context (il lit le
# champ du kubeconfig) : on affiche l'override quand il existe.
echo "== Secret de push registre =="
echo "   cluster   : ${KUBE_CONTEXT:-$(kubectl config current-context)}"
echo "   namespace : $NAMESPACE"
echo "   secret    : $SECRET_NAME"
echo "   registre  : $REGISTRY_HOST (user: $REGISTRY_USERNAME)"

"${KCTL[@]}" create secret docker-registry "$SECRET_NAME" \
  -n "$NAMESPACE" \
  --docker-server="$REGISTRY_HOST" \
  --docker-username="$REGISTRY_USERNAME" \
  --docker-password="$SCW_SECRET_KEY" \
  --dry-run=client -o yaml | "${KCTL[@]}" apply -f -

echo "== OK — le Job de build peut désormais pousser sur $REGISTRY_HOST =="
