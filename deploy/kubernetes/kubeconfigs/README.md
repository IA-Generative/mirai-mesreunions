# Kubeconfigs

Ce dossier contient les **kubeconfigs locaux** des clusters cibles. Le contenu de ce dossier
est **gitignoré** : aucun kubeconfig ne doit jamais être commité (ils contiennent les
certificats du cluster + un token utilisateur valide qui constitue un secret).

## Conventions de nommage

Un fichier par cluster, nom = identifiant exact du cluster :

| Cluster | Fichier attendu | Environnement | Zone CDS |
|---|---|---|---|
| `<staging-cluster>` | `<staging-cluster>.kubeconfig` | staging (Scaleway) | externe + interne (single-cluster) |
| `<external-cluster>` | `<external-cluster>.kubeconfig` | mirai (prod-bêta) | **à déterminer** par test SSO |
| `<internal-cluster>` | `<internal-cluster>.kubeconfig` | mirai (prod-bêta) | **à déterminer** par test SSO |

## Où récupérer les kubeconfigs

- **Scaleway Console** → Kubernetes → cluster → onglet *Configurer kubectl* → copier le YAML
- Sauvegarder sous `deploy/kubernetes/kubeconfigs/<nom-cluster>.kubeconfig`
- Permissions recommandées : `chmod 600` (les kubeconfigs sont des secrets utilisateur)

## Utilisation

```bash
# Pointer kubectl sur un cluster spécifique pour cette session
export KUBECONFIG="$PWD/deploy/kubernetes/kubeconfigs/<external-cluster>.kubeconfig"
kubectl config current-context
kubectl get nodes

# Sans modifier l'env global (one-shot)
kubectl --kubeconfig deploy/kubernetes/kubeconfigs/<internal-cluster>.kubeconfig get nodes
```

## Mapping cluster ↔ zone CDS

L'architecture cible Mirai prod-bêta repose sur deux clusters réseau-isolés implémentant le
pattern Cross Domain Solution (cf. [`docs/REVIEW_RESPONSE_PLAN.md`](../../../docs/REVIEW_RESPONSE_PLAN.md)).
**L'un des deux clusters peut atteindre `<sso-host>`, l'autre non**. Cette
asymétrie d'égress identifie quelle moitié de la passerelle joue chaque zone.

Pour déterminer le mapping, exécuter :

```bash
./deploy/kubernetes/scripts/check-cluster-sso.sh \
  deploy/kubernetes/kubeconfigs/<external-cluster>.kubeconfig
./deploy/kubernetes/scripts/check-cluster-sso.sh \
  deploy/kubernetes/kubeconfigs/<internal-cluster>.kubeconfig
```

Une fois le résultat connu, mettre à jour `deploy/kubernetes/environments/mirai/README.md`
avec le mapping définitif.

## Sécurité

- Ne JAMAIS commiter un kubeconfig (le `.gitignore` bloque, mais une force-add `git add -f`
  passerait — ne le fais pas)
- Ne JAMAIS partager un kubeconfig en clair (Slack, mail, capture d'écran)
- Révoquer côté Scaleway si fuite suspectée
- En cas de doute, regénérer côté Scaleway et écraser localement
