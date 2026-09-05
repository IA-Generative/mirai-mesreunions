# Environments

Ce dossier décrit les **cibles de déploiement** du projet, et — depuis le 2026-08-30 — il
héberge les manifestes de prod-bêta qui n'avaient de source nulle part.

## Vue d'ensemble

| # | Environnement | Type | Topologie | Zones CDS |
|---|---|---|---|---|
| 1 | `local` | Docker Compose (ARM/x86) | 1 hôte | externe + interne en namespaces réseau Docker |
| 2 | `staging` | Kubernetes managé | 1 cluster | externe + interne dans un seul cluster (deux namespaces, séparation par NetworkPolicy) |
| 3 | `prod-bêta` | Kubernetes managé | 2 clusters réseau-isolés | un cluster pour la zone externe, un autre pour la zone interne — séparation réseau réelle |

Les noms et identifiants concrets de clusters, ainsi que les hôtes cibles, ne sont **pas**
documentés dans ce dépôt public. Ils vivent dans des fichiers gitignorés (`*.local.yaml`,
`*.local.md`).

## ⚠ Où vivent réellement les manifestes de prod-bêta (état mesuré le 2026-08-30)

**Ce README a longtemps renvoyé à `deploy/kubernetes/internal-zone/` et
`external-zone/`. Ces dossiers ne sont pas dans le dépôt** : ils ont été retirés au
`filter-repo` du 2026-05-20 (ils portaient des hôtes et des adresses privées), sont
gitignorés, et ne sont synchronisés que vers la machine de construction par le `rsync` de
`deploy/scripts/commit-push-build.sh` quand `REMOTE_HOST` est défini. Un clone frais ne les
a pas. La correction de ce README fait partie du même chantier que le tableau ci-dessous.

Couverture réelle des huit charges de `audio-internal` :

| Charge | Source du manifeste |
|---|---|
| `device-token-authority`, `internal-ingester`, `transcription-relay` | `internal-zone/deployments.yaml` — hors dépôt (machine de construction) ⚠ **dérive grave, voir plus bas** |
| `video-ingest-api`, `video-ingest-mcp`, `video-ingest-worker` | [`prod-beta/internal/video-ingest.yaml`](prod-beta/README.md) — **versionné ici depuis le 2026-08-30** |
| `mesreunions-web`, `admin-console` | **aucune — encore fantômes** |

**Piège de lecture** : `external-zone/deployments.yaml` contient bien des entrées nommées
`mesreunions-web` et `admin-console`, mais en namespace **`audio-external`** — ce sont les
charges de l'AUTRE cluster. Un `grep` sur le seul nom fait croire à tort que celles de
`audio-internal` sont couvertes. Toujours vérifier le namespace.

## ⛔ Ne jamais appliquer `internal-zone/deployments.yaml` tel quel

C'est une **base minimale et volontairement inerte** (transcription en `stub`, options
Kevent éteintes) pour qu'un développeur puisse la copier sans casser son poste. La
configuration réelle de prod-bêta était portée par un overlay kustomize —
**qui n'existe plus** : `git log --all` prouve qu'il n'a jamais été committé, et la machine
de construction ne l'a pas non plus.

Conséquence : **les valeurs de prod-bêta ne survivent que dans les pods en cours
d'exécution.** Mesuré le 2026-08-30 par `kubectl diff`, un `apply` de la base ferait :

- `TRANSCRIPTION_BACKEND` : `kevent` → **`stub`** (plus aucune transcription réelle)
- les sept `KEVENT_*_ENABLED` : `true` → **`false`**
- `KEVENT_GATEWAY_URL`, `LITELLM_BASE_URL`, `MCR_GATEWAY_URL`, `OIDC_TOKEN_ENDPOINT` → **vides**
- `LLM_HTTP_TIMEOUT_SECONDS` → **supprimée**
- `DEVICE_TOKEN_RETENTION_HOURS` : `360` → `168` (7 jours au lieu de 15)
- `RABBITMQ_HOST` → un nom DNS qui ne résout pas depuis ce cluster

Pour un changement ciblé sur ces trois charges, utiliser `kubectl set image` / `kubectl set
env` : chirurgical, ne touche rien d'autre. La marche à suivre complète, et comment
reconstruire un manifeste perdu, sont dans
[`docs/RUNBOOK_DEPLOIEMENT_PROD_BETA.md`](../../../docs/RUNBOOK_DEPLOIEMENT_PROD_BETA.md).

## Conventions

- Les **kubeconfigs** sont locaux et gitignorés ; voir [`../kubeconfigs/README.md`](../kubeconfigs/README.md).
- Les **secrets** ne sont jamais commités : un `secrets.<env>.local.yaml` est attendu par environnement.
- Les **hôtes réels** ne sont jamais commités. Les manifestes versionnés portent la
  convention `*.fake-domain.name` / `example.com` et exigent une surcharge gitignorée
  (cf. [ADR-0005](../../../docs/adr/0005-manifestes-versionnes-hotes-expurges.md)).
- Les **images** sont épinglées sur un tag immuable ; `latest` est banni
  (cf. [ADR-0006](../../../docs/adr/0006-images-epinglees-latest-banni.md)).

## Pourquoi deux clusters pour `prod-bêta`

L'architecture implémente le patron **Cross Domain Solution** (vocabulaire ANSSI : *rupture
protocolaire avec dépôt sur guichet*). La séparation réseau-réelle entre les deux clusters
matérialise la rupture de flux : aucun lien entrant DMZ → interne, transferts initiés
exclusivement depuis la zone la plus sensible (PULL via le bucket S3 de dépôt).

En `staging` (cluster unique), les deux zones cohabitent sous deux namespaces : suffisant
pour la validation fonctionnelle, mais ne reproduit pas l'isolation réseau de la cible.

## Voir aussi

- `local` → `deploy/docker/docker-compose.yml`
- [`staging/`](staging/README.md) · [`prod-beta/`](prod-beta/README.md)
