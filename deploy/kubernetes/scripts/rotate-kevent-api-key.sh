#!/usr/bin/env bash
# Remplace la clé de la passerelle MirAI (Kevent) en prod-bêta et PROUVE qu'elle marche.
#
# Contexte : le 2026-09-16, GET /jobs répondait 401 {"error":"token inactive"} — la même
# clé dépose les jobs de transcription, donc aucun audio ne pouvait plus être transcrit.
# Le jeton est un jeton applicatif « Production » (Zitadel, 1 an), obtenu via le canal
# support MirAI. Il n'existe nulle part ailleurs : ni dans un autre namespace, ni sur un
# poste. Ce script ne le fabrique pas, il le POSE et le VÉRIFIE.
#
# Usage :
#   KEVENT_NEW_TOKEN='xxxx' deploy/kubernetes/scripts/rotate-kevent-api-key.sh
#   (ou sans variable : le jeton est demandé au clavier, sans écho)
#
# Étapes : 1) contrôle du jeton DEPUIS UN POD (la passerelle est filtrée par IP),
#          2) sauvegarde du Secret, 3) mise à jour de la clé `kevent_api_key` seule
#          (`litellm_api_key` est conservée), 4) redémarrage d'internal-ingester,
#          5) contrôle final depuis le nouveau pod : GET /jobs → 200.
set -euo pipefail

NS="${NS:-audio-internal}"
SECRET="${SECRET:-kevent-api-key}"
DEPLOY="${DEPLOY:-internal-ingester}"          # celui qui sert de sonde
# Toutes les charges qui lisent la clé `kevent_api_key` : elles la reçoivent par
# variable d'environnement, donc un redémarrage est nécessaire pour chacune.
# Relevé le 2026-09-17 : internal-ingester (KEVENT_API_KEY) et les trois charges
# video-ingest (VIDEO_INGEST_KEVENT_API_KEY). En oublier une la laisse sur
# l'ancienne clé, sans le moindre signe.
CONSOMMATEURS="${CONSOMMATEURS:-internal-ingester video-ingest-api video-ingest-mcp video-ingest-worker}"
# Contexte kubectl : CELUI EN COURS par défaut. Figer un nom était une erreur —
# il change d'un poste, d'un terminal et d'un kubeconfig à l'autre, et le script
# rendait alors « context does not exist » (mesuré le 2026-09-17). On ne fait
# plus confiance à un NOM : on vérifie que le cluster joint porte bien la charge
# visée, ce qui est la seule chose qui compte.
CTX="${KUBE_CONTEXT:-}"
if [ -n "$CTX" ]; then
  K="kubectl --context=$CTX -n $NS"
else
  K="kubectl -n $NS"
fi

# ── 0. Sommes-nous sur le bon cluster ? ──────────────────────────────────────
if ! $K get deploy "$DEPLOY" >/dev/null 2>&1; then
  echo "⛔ La charge « $DEPLOY » est introuvable dans le namespace « $NS »." >&2
  echo "   Le contexte kubectl en cours ne vise pas le cluster interne." >&2
  echo >&2
  echo "   Contexte en cours : $(kubectl config current-context 2>/dev/null || echo '(aucun)')" >&2
  echo "   KUBECONFIG        : ${KUBECONFIG:-(défaut ~/.kube/config)}" >&2
  echo >&2
  echo "   Contextes disponibles :" >&2
  kubectl config get-contexts 2>&1 | sed 's/^/     /' >&2
  echo >&2
  echo "   Relancer en désignant le bon contexte :" >&2
  echo "     KUBE_CONTEXT=<nom> $0" >&2
  exit 3
fi

# Nommer le contexte RÉELLEMENT utilisé : `kubectl config current-context` ignore
# l'option --context et afficherait un autre nom que celui qui travaille.
SERVEUR="$($K config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
CTX_UTILISE="${CTX:-$(kubectl config current-context 2>/dev/null || echo '?')}"
echo "Cluster visé : $CTX_UTILISE ${SERVEUR:+($SERVEUR)}"
echo "Charge sonde : $NS/$DEPLOY"
echo

if [ -z "${KEVENT_NEW_TOKEN:-}" ]; then
  read -r -s -p "Nouveau jeton de la passerelle (sans « Bearer ») : " KEVENT_NEW_TOKEN; echo
fi
# Un copier-coller apporte souvent un espace, un retour chariot ou le préfixe
# « Bearer » : on nettoie, sinon la passerelle refuserait un jeton pourtant bon.
KEVENT_NEW_TOKEN="$(printf '%s' "$KEVENT_NEW_TOKEN" | tr -d '[:space:]')"
KEVENT_NEW_TOKEN="${KEVENT_NEW_TOKEN#Bearer}"
[ -n "$KEVENT_NEW_TOKEN" ] || { echo "jeton vide" >&2; exit 2; }

# Programme de contrôle joué DANS le pod : lui seul voit la passerelle, qui
# filtre par adresse IP. Le jeton arrive par l'entrée standard, jamais en
# argument — une ligne de commande se retrouve dans le journal d'audit du
# cluster et dans la liste des processus du conteneur.
LECTEUR='
import os, sys, requests
tok = sys.stdin.read().strip() or os.environ.get("KEVENT_API_KEY", "")
tok = tok[len("Bearer "):] if tok.startswith("Bearer ") else tok
url = os.environ["KEVENT_GATEWAY_URL"].rstrip("/") + "/jobs?limit=1"
try:
    r = requests.get(url, headers={"Authorization": "Bearer " + tok}, timeout=20)
except Exception as exc:
    print("INJOIGNABLE", exc); sys.exit(2)
print(r.status_code, (r.text or "")[:200])
sys.exit(0 if r.status_code == 200 else 1)
'

# ⚠ `-i` est OBLIGATOIRE. Sans lui, kubectl n'ouvre pas l'entrée standard du
# conteneur : le programme arrive VIDE, python ne fait rien et rend 0. Le
# contrôle réussissait alors sans avoir rien contrôlé, et le moindre raté de
# `kubectl exec` passait pour un refus de la passerelle — c'est ce qui a accusé
# un jeton à tort le 2026-09-17.
probe() {  # $1 = jeton à tester ; vide = celui déjà en service dans le pod
  # `grep -v` retire le « command terminated with exit code N » que kubectl
  # ajoute sur sa sortie d'erreur : il brouille la réponse de la passerelle
  # sans rien apprendre (le code de retour, lui, est conservé par PIPESTATUS).
  printf '%s' "${1:-}" \
    | $K exec -i deploy/"$DEPLOY" -- python -c "$LECTEUR" 2>&1 \
    | grep -v '^command terminated with exit code'
  return "${PIPESTATUS[1]}"
}

echo "1) contrôle du nouveau jeton depuis le pod ${DEPLOY}…"
if REPONSE="$(probe "$KEVENT_NEW_TOKEN" 2>&1)"; then CODE=0; else CODE=$?; fi
echo "   réponse de la passerelle : ${REPONSE:-<aucune>}"
if [ $CODE -ne 0 ]; then
  case "$REPONSE" in
    *"token inactive"*) echo "   ✗ jeton REFUSÉ (« token inactive ») : il n'est pas actif côté passerelle." >&2 ;;
    *INJOIGNABLE*)      echo "   ✗ passerelle INJOIGNABLE depuis le pod — le jeton n'est pas en cause." >&2 ;;
    ""|*rror*)          echo "   ✗ le contrôle n'a PAS PU être joué (accès au pod ?) — le jeton n'est pas en cause." >&2 ;;
    *)                  echo "   ✗ la passerelle refuse ce jeton." >&2 ;;
  esac
  echo "   rien n'est modifié." >&2
  exit 1
fi

TS="$(date +%Y%m%d-%H%M%S)"
BK="private/secret-${SECRET}-${TS}.yaml"
mkdir -p private
echo "2) sauvegarde de l'ancien Secret → $BK (dossier ignoré par git)"
$K get secret "$SECRET" -o yaml > "$BK"

echo "3) mise à jour de la clé kevent_api_key"
B64="$(printf '%s' "$KEVENT_NEW_TOKEN" | base64 | tr -d '\n')"
$K patch secret "$SECRET" --type merge -p "{\"data\":{\"kevent_api_key\":\"$B64\"}}"

echo "4) redémarrage des charges qui lisent la clé (elle est lue au démarrage)"
for d in $CONSOMMATEURS; do
  $K get deploy "$d" >/dev/null 2>&1 || { echo "   – $d absent, ignoré"; continue; }
  echo "   – $d"
  $K rollout restart deploy/"$d"
done
for d in $CONSOMMATEURS; do
  $K get deploy "$d" >/dev/null 2>&1 || continue
  $K rollout status deploy/"$d" --timeout=180s
done

echo "5) contrôle final : le pod utilise-t-il bien la nouvelle clé ?"
# Entrée standard vide : le programme retombe sur la clé lue dans l'environnement
# du pod, c'est-à-dire celle que le Secret vient de lui donner.
if REPONSE="$(probe "" 2>&1)"; then CODE=0; else CODE=$?; fi
echo "   réponse de la passerelle : ${REPONSE:-<aucune>}"
[ $CODE -eq 0 ] || { echo "   ✗ la nouvelle clé n'est pas acceptée depuis le pod." >&2; exit 1; }
echo "✓ clé en service. Le mot « token inactive » ne doit plus apparaître dans les journaux d'$DEPLOY."
echo "  Penser à mettre à jour MIRAI_GATEWAY_TOKEN dans mirai-apps-beta-private/private/credentials.env."
echo
echo "  ⚠ Ne PAS toucher à la clé « litellm_api_key » du même Secret : c'est le jeton du"
echo "    hub d'inférence, il fonctionne, et il est lu aussi par mesreunions-web."
