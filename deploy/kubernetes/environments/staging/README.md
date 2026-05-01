# Staging

Cluster Kubernetes managé unique hébergeant **les deux zones** dans deux namespaces
séparés. Cible de validation fonctionnelle avant déploiement prod-bêta.

> Les noms concrets du cluster, son ID provider, ses LoadBalancers et la conf WireGuard
> qui y vit ne sont **pas** documentés ici car le repo est public. Ils vivent dans
> `CLUSTER_DETAILS.local.md` (gitignoré, à créer localement).

## Topologie

| Champ | Valeur |
|---|---|
| Type | Kubernetes managé (provider à documenter localement) |
| Région | `fr-par` |
| Kubeconfig (local, gitignoré) | `deploy/kubernetes/kubeconfigs/<staging>.yaml` |
| Namespaces | `audio-external`, `audio-internal` |

## Particularités vs. base

- **Cluster unique** : la séparation est uniquement par namespace + NetworkPolicies, pas
  réelle au niveau réseau. Acceptable pour staging, pas pour prod.
- Une instance Postgres par zone (deux Helm releases distinctes ou deux StatefulSets).
- Ingress `nginx.ingress.kubernetes.io/proxy-body-size: 256m` (déjà cohérent avec
  `deploy/kubernetes/external-zone/deployments.yaml`).
- Un namespace tiers héberge un proxy WireGuard qui sert de pattern de référence pour
  l'intégration prod-bêta vers la cible. Voir `CLUSTER_DETAILS.local.md` pour les détails
  d'implémentation.

## Apply

```bash
export KUBECONFIG="$PWD/deploy/kubernetes/kubeconfigs/<staging>.yaml"

# Vérifier le contexte
kubectl config current-context
kubectl get nodes

# Secrets (jamais commités — obligatoire avant apply)
kubectl apply -f deploy/kubernetes/shared/secrets.staging.local.yaml

# Manifestes externe + interne
kubectl apply -f deploy/kubernetes/external-zone/
kubectl apply -f deploy/kubernetes/internal-zone/
```

## Tests post-apply

- `kubectl get pods -A | grep -E 'audio-external|audio-internal'`
- Healthchecks : `kubectl exec -n audio-external <pod> -- curl -s localhost:<port>/healthz`
- Connectivité OIDC : depuis un pod du namespace externe,
  `curl -sI https://<sso-host>/.well-known/openid-configuration`
  (la valeur `<sso-host>` vit dans `CLUSTER_DETAILS.local.md`)
