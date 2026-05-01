# Environments

Ce dossier décrit les **cibles de déploiement** du projet. Les manifestes de base
(`deploy/kubernetes/external-zone/`, `internal-zone/`, `shared/`) ne sont pas dupliqués
ici : chaque environnement documente uniquement ses **différences** par rapport à la
base et la procédure d'application.

## Vue d'ensemble

| # | Environnement | Type | Cluster(s) | Zones CDS |
|---|---|---|---|---|
| 1 | `local` | Docker Compose (ARM/x86) | n/a | externe + interne en namespaces réseau Docker |
| 2 | `staging` | Kubernetes Scaleway | `<staging-cluster>` | externe + interne dans un seul cluster (deux namespaces) |
| 3 | `mirai` (prod-bêta) | Kubernetes Scaleway | `<external-cluster>` **+** `<internal-cluster>` | externe sur un cluster, interne sur l'autre — séparation réseau réelle |

## Pourquoi deux clusters pour `mirai`

L'architecture cible implémente le pattern **Cross Domain Solution** (vocabulaire ANSSI :
*rupture protocolaire avec dépôt sur guichet*) — voir
[`docs/REVIEW_RESPONSE_PLAN.md`](../../../docs/REVIEW_RESPONSE_PLAN.md) pour les références
doctrinales (PG-075, PA-066, Eurydice ; NIST SC-7/AC-4 ; NSA RAIN ; Cloud π Native).

La séparation **réseau-réelle** entre les deux clusters Kubernetes Scaleway matérialise la
rupture de flux : aucun lien réseau entrant DMZ → interne, transferts de données initiés
exclusivement depuis la zone la plus sensible (PULL via S3 `processed-staging`).

En `staging` (cluster unique), les deux zones cohabitent dans un même cluster sous deux
namespaces : c'est suffisant pour la validation fonctionnelle, mais ne reproduit pas
l'isolation réseau de la cible prod-bêta.

## Mapping cluster ↔ zone — à confirmer par test

Pour `mirai`, il reste à déterminer **quel cluster joue quelle zone**. Méthode :

L'un des deux clusters doit pouvoir atteindre `<sso-host>` (SSO Keycloak du
ministère, utilisé par les portails OIDC) ; l'autre ne doit pas avoir cet égress. Le cluster
qui peut atteindre le SSO **est la zone externe** (admin-portal, code-generator, upload-portal,
antivirus-worker, transcode-worker, file-mover). L'autre **est la zone interne** (token-issuer,
file-puller, transcription-stub).

Exécuter le test depuis chaque cluster :

```bash
./deploy/kubernetes/scripts/check-cluster-sso.sh \
  deploy/kubernetes/kubeconfigs/<external-cluster>.kubeconfig

./deploy/kubernetes/scripts/check-cluster-sso.sh \
  deploy/kubernetes/kubeconfigs/<internal-cluster>.kubeconfig
```

Une fois le mapping connu, le reporter dans [`mirai/README.md`](mirai/README.md).

## Conventions

- Les **kubeconfigs** sont locaux et gitignorés ; voir
  [`../kubeconfigs/README.md`](../kubeconfigs/README.md).
- Les **secrets** (Keycloak, S3, RabbitMQ, internal API token) ne sont **jamais** commités ;
  un fichier `secrets.<env>.local.yaml` est attendu par environnement, gitignoré.
- Les manifestes de base sous `deploy/kubernetes/{external,internal}-zone/` doivent rester
  **déployables tels quels** (en ajustant uniquement images, replicas, env vars par patch ou
  par variables d'environnement à l'apply).

## Voir aussi

- [`local`](#) → `deploy/docker/docker-compose.yml`
- [`staging/`](staging/README.md)
- [`mirai/`](mirai/README.md)
