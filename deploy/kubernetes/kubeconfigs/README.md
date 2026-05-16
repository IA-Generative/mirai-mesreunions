# Kubeconfigs

Ce dossier contient les **kubeconfigs locaux** des clusters cibles. Le contenu de ce
dossier est **gitignoré** : aucun kubeconfig ne doit jamais être commité (ils contiennent
les certificats du cluster + un token utilisateur valide qui constitue un secret).

## Conventions de nommage

Un fichier par cluster, nommage descriptif et **abstrait** (pas le nom natif du provider
si celui-ci révèle l'environnement) :

| Cible | Fichier suggéré |
|---|---|
| staging | `deploy/kubernetes/kubeconfigs/<kubeconfig-staging>.yaml` |
| prod-bêta — cluster externe | `deploy/kubernetes/kubeconfigs/<kubeconfig-external>.yaml` |
| prod-bêta — cluster interne | `deploy/kubernetes/kubeconfigs/<kubeconfig-internal>.yaml` |

Les noms exacts des fichiers locaux n'ont pas d'importance fonctionnelle ; seule l'option
`--kubeconfig` ou la variable `KUBECONFIG` doit pointer dessus. Évite simplement les noms
qui révèlent l'environnement (par ex. les noms whimsy auto-générés par certains providers).

## Où récupérer les kubeconfigs

- **Console du provider** → Kubernetes → cluster → onglet *Configurer kubectl* → copier
  le YAML
- Ou via la CLI du provider (`scw k8s kubeconfig get <id>`, `gcloud …`, `eksctl …`)
- Sauvegarder sous `deploy/kubernetes/kubeconfigs/<nom-descriptif>.yaml`
- Permissions recommandées : `chmod 600` (les kubeconfigs sont des secrets utilisateur)

## Utilisation

```bash
# Pointer kubectl sur un cluster spécifique pour cette session
export KUBECONFIG="$PWD/deploy/kubernetes/kubeconfigs/<kubeconfig>.yaml"
kubectl config current-context
kubectl get nodes

# Sans modifier l'env global (one-shot)
kubectl --kubeconfig deploy/kubernetes/kubeconfigs/<kubeconfig>.yaml get nodes
```

## Mapping cluster ↔ zone CDS

L'architecture cible prod-bêta repose sur deux clusters réseau-isolés implémentant le
pattern Cross Domain Solution (cf.
[`docs/REVIEW_RESPONSE_PLAN.md`](../../../docs/REVIEW_RESPONSE_PLAN.md)).

- **Cluster externe (DMZ)** : héberge admin-console, mydevices-web, mobile-upload-pwa,
  clamav-scanner, audio-normalizer, dmz-to-internal-bridge. Doit pouvoir atteindre les services
  d'authentification de la cible (typiquement via tunnel WireGuard).
- **Cluster interne** : héberge device-token-authority, internal-ingester, transcription. Égress
  restreint, ACL API server limitée à la whitelist ministérielle.

Le mapping concret (qui est externe, qui est interne) ainsi que les détails de
configuration (UUIDs, hôtes cibles, sous-réseaux, IPs whitelistées) vivent dans le
fichier `deploy/kubernetes/environments/prod-beta/CLUSTER_DETAILS.local.md` (gitignoré).

## Sécurité

- Ne JAMAIS commiter un kubeconfig (le `.gitignore` bloque, mais une force-add
  `git add -f` passerait — ne le fais pas)
- Ne JAMAIS partager un kubeconfig en clair (Slack, mail, capture d'écran)
- Révoquer côté provider si fuite suspectée
- En cas de doute, regénérer côté provider et écraser localement
