# Environments

Ce dossier décrit les **cibles de déploiement** du projet. Les manifestes de base
(`deploy/kubernetes/external-zone/`, `internal-zone/`, `shared/`) ne sont pas dupliqués
ici : chaque environnement documente uniquement ses **différences** par rapport à la
base et la procédure d'application.

## Vue d'ensemble

| # | Environnement | Type | Topologie | Zones CDS |
|---|---|---|---|---|
| 1 | `local` | Docker Compose (ARM/x86) | 1 hôte | externe + interne en namespaces réseau Docker |
| 2 | `staging` | Kubernetes managé | 1 cluster | externe + interne dans un seul cluster (deux namespaces, séparation par NetworkPolicy) |
| 3 | `prod-bêta` | Kubernetes managé | 2 clusters réseau-isolés | un cluster pour la zone externe, un autre pour la zone interne — séparation réseau réelle |

Les noms et identifiants concrets de clusters, ainsi que les hôtes cibles d'intégration,
ne sont **pas** documentés dans ce repo public. Ils vivent dans des fichiers `*.local.md`
gitignorés sous chaque sous-dossier d'environnement (cf. exemple
`environments/prod-beta/CLUSTER_DETAILS.local.md` à créer localement).

## Pourquoi deux clusters pour `prod-bêta`

L'architecture cible implémente le pattern **Cross Domain Solution** (vocabulaire ANSSI :
*rupture protocolaire avec dépôt sur guichet*) — voir
[`docs/REVIEW_RESPONSE_PLAN.md`](../../../docs/REVIEW_RESPONSE_PLAN.md) pour les références
doctrinales (PG-075, PA-066, Eurydice ; NIST SC-7/AC-4 ; NSA RAIN ; Cloud π Native).

La séparation **réseau-réelle** entre les deux clusters Kubernetes matérialise la rupture
de flux : aucun lien réseau entrant DMZ → interne, transferts de données initiés
exclusivement depuis la zone la plus sensible (PULL via le bucket S3 de dépôt).

En `staging` (cluster unique), les deux zones cohabitent dans un même cluster sous deux
namespaces : c'est suffisant pour la validation fonctionnelle, mais ne reproduit pas
l'isolation réseau de la cible prod-bêta.

## Mapping cluster ↔ zone (prod-bêta)

Le mapping concret (lequel des deux clusters joue la zone externe vs. la zone interne)
dépend des contraintes opérationnelles de l'infrastructure d'accueil :

- **Zone externe** : cluster qui peut atteindre les services d'authentification de la
  cible d'intégration (OIDC / SSO). Hébergera un proxy WireGuard ou équivalent vers le
  réseau de la cible. Reçoit le trafic public via Ingress / LoadBalancer.
- **Zone interne** : cluster avec égress restreint (ACL API server, NetworkPolicies) ;
  ne parle qu'au PostgreSQL et S3 internes, et tire les fichiers depuis le bucket de
  dépôt commun.

Critère de choix entre les deux candidats : capacité à établir une connexion vers le SSO
de la cible. Le script `scripts/check-cluster-sso.sh` aide à valider la connectivité une
fois les routes (VPN, peering, whitelist) en place.

## Conventions

- Les **kubeconfigs** sont locaux et gitignorés ; voir
  [`../kubeconfigs/README.md`](../kubeconfigs/README.md).
- Les **secrets** (Keycloak, S3, RabbitMQ, internal API token) ne sont **jamais** commités ;
  un fichier `secrets.<env>.local.yaml` est attendu par environnement, gitignoré.
- Les **détails d'infrastructure** (noms de clusters, UUIDs, IPs, sous-réseaux, hôtes
  cibles) ne sont **jamais** commités ; un fichier `<env>/CLUSTER_DETAILS.local.md` est
  attendu, gitignoré (pattern `*.local.md`).
- Les manifestes de base sous `deploy/kubernetes/{external,internal}-zone/` doivent rester
  **déployables tels quels** (en ajustant uniquement images, replicas, env vars par patch
  ou par variables d'environnement à l'apply).

## Voir aussi

- `local` → `deploy/docker/docker-compose.yml`
- [`staging/`](staging/README.md)
- [`prod-beta/`](prod-beta/README.md)
