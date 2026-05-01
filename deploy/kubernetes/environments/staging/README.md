# Staging — `<staging-cluster>`

Cluster Scaleway unique hébergeant **les deux zones** dans deux namespaces séparés.
Cible de validation fonctionnelle avant déploiement Mirai prod-bêta.

## Cluster

| Champ | Valeur |
|---|---|
| Nom Scaleway | `<staging-cluster>` |
| Région | `fr-par` (Paris) |
| Kubeconfig attendu | `deploy/kubernetes/kubeconfigs/<staging-cluster>.kubeconfig` |
| Namespaces | `audio-external`, `audio-internal` |

## Particularités vs. base

- Cluster unique : la séparation est uniquement par namespace + NetworkPolicies, **pas
  réelle au niveau réseau**. Acceptable pour staging, pas pour prod.
- Une instance Postgres par zone (deux Helm releases distinctes ou deux StatefulSets)
- Ingress `nginx.ingress.kubernetes.io/proxy-body-size: 256m` (déjà cohérent avec
  `deploy/kubernetes/external-zone/deployments.yaml`)

## Apply

```bash
export KUBECONFIG="$PWD/deploy/kubernetes/kubeconfigs/<staging-cluster>.kubeconfig"

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
- Connectivité OIDC : depuis un pod du namespace externe, `curl -sI https://<sso-staging>/.well-known/openid-configuration`
