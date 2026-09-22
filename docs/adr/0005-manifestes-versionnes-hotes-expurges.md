# ADR-0005 — Manifestes de prod-bêta versionnés dans un dépôt public, hôtes expurgés

## Statut
Accepté — 2026-08-30

## Contexte

Le 2026-08-30, `video-ingest-mcp` est en `CrashLoopBackOff` : 68 redémarrages en 5 h 27,
première source d'erreurs du cluster ce jour-là. En cherchant où corriger, on découvre que
les trois charges `video-ingest-api`, `-mcp` et `-worker` **n'ont de manifeste nulle part** :

- `git log --all` sur `deploy/kubernetes/environments/` ne connaît que `README.md` et
  `staging/README.md` : le dossier `prod-beta/` n'a **jamais** été committé, alors que
  `features-2-build/FEATURE_video-ingest.md` en nomme le chemin depuis le 2026-05-25 ;
- sur la machine de construction, `internal-zone/deployments.yaml` contient **zéro**
  occurrence de « video-ingest » ;
- les objets du cluster portent une annotation `last-applied-configuration` : ils ont été
  posés à la main, et **c'est la seule description qui subsiste**.

Tant que cela reste vrai, tout correctif appliqué au cluster disparaît au prochain `apply`,
sans un mot. Corriger la panne avant de régler cela aurait été bâtir sur du sable.

Une contrainte s'y oppose : `IA-Generative/mirai-mesreunions` est un dépôt **public**. Les
dossiers `internal-zone/` et `external-zone/` en ont précisément été retirés au
`filter-repo` du 2026-05-20 parce qu'ils portaient des hôtes et des adresses privées. Les
manifestes de `video-ingest` portent deux hôtes réels — le SSO ministériel et la passerelle
de transcription — dont on a vérifié qu'ils n'apparaissaient **nulle part** dans le dépôt
public avant ce jour (`grep` = 0 occurrence). Les écrire tels quels serait une fuite neuve.

## Options considérées

### Option A — reconstruire dans `internal-zone/`, gitignoré

Rejoint les cinq autres charges et respecte la règle maison « ne jamais re-tracker ces
dossiers ».

- **Contre, dirimant** : cela ne résout rien. Le manifeste resterait hors git, sans
  historique, sans revue, sans garantie qu'il existe encore dans six mois — c'est
  exactement l'état qui a produit le problème. Un fichier sur une VM n'est pas une source.

### Option B — versionner tel quel dans le dépôt public

- **Contre, dirimant** : publie deux hôtes d'infrastructure ministérielle. Non.

### Option C — versionner expurgé, avec surcharge gitignorée obligatoire *(retenue)*

Le manifeste versionné porte la convention `*.fake-domain.name` / `example.com`, déjà
employée partout dans le dépôt — et prescrite pour ces variables précises par
`services/video_ingest/SECURITY_REVIEW.md`. Les valeurs réelles vivent dans
`patch-hotes.local.yaml`, gitignoré par la convention `*.local.yaml`, que la
`kustomization.yaml` **référence explicitement**.

## Décision

**Option C.** Les manifestes entrent dans `deploy/kubernetes/environments/prod-beta/internal/`,
expurgés, accompagnés d'un gabarit `patch-hotes.local.yaml.example` versionné.

Deux choix méritent d'être explicités, parce qu'ils vont à contre-courant du réflexe :

1. **La surcharge est exigée, pas optionnelle.** Sans `patch-hotes.local.yaml`,
   `kustomize build` **échoue** avec « no such file ». C'est délibéré : un déploiement avec
   la mauvaise adresse de SSO casserait l'authentification de tous les imports sans que rien
   ne le dise. Un refus bruyant vaut mieux qu'un déploiement faux. Le repli silencieux — se
   rabattre sur l'hôte expurgé — a été écarté pour cette raison exacte.

2. **L'ordre des variables d'environnement suit la fusion kustomize.** Une fusion
   stratégique remonte toujours les éléments patchés en tête de liste. La base est donc
   écrite dans cet ordre, ce qui rend `kubectl diff` **vide** après application. Sans cela,
   chaque `apply` aurait rejoué un déploiement pour un simple changement d'ordre sans effet
   — et le bruit aurait fini par masquer les vrais écarts.

Le critère d'acceptation est **`kubectl diff` à zéro écart** : un manifeste qui ne décrit
pas exactement ce qui tourne n'est pas une source, c'est une opinion. Vérifié le
2026-08-30 après application.

## Conséquences

- Les trois charges `video-ingest-*` ont une source relue, historisée, et prouvée conforme.
- Un clone frais ne peut pas déployer prod-bêta sans qu'on lui remette la surcharge — c'est
  la propriété recherchée, pas un défaut.
- **La dette n'est pas soldée** : `mesreunions-web` et `admin-console` de `audio-internal`
  n'ont toujours aucun manifeste, et l'overlay qui portait les ~17 variables des trois
  charges restantes a disparu. Leur configuration ne survit que dans les pods en cours
  d'exécution. Voir [`RUNBOOK_DEPLOIEMENT_PROD_BETA.md`](../RUNBOOK_DEPLOIEMENT_PROD_BETA.md).
- Le contrôle anti-fuite est celui déjà en place : `gitleaks` en `pre-commit` (passé), plus
  une relecture explicite qu'aucun hôte réel n'entre dans un fichier suivi.

## Mise en œuvre

| Fichier | Rôle |
|---|---|
| [`environments/prod-beta/internal/video-ingest.yaml`](../../deploy/kubernetes/environments/prod-beta/internal/video-ingest.yaml) | 3 Deployments, 2 Services, 1 CiliumNetworkPolicy — hôtes expurgés |
| [`environments/prod-beta/internal/kustomization.yaml`](../../deploy/kubernetes/environments/prod-beta/internal/kustomization.yaml) | Assemble la base, exige la surcharge, épingle le tag d'image |
| `environments/prod-beta/internal/patch-hotes.local.yaml` | **Gitignoré** — les deux hôtes réels |
| [`…patch-hotes.local.yaml.example`](../../deploy/kubernetes/environments/prod-beta/internal/patch-hotes.local.yaml.example) | Gabarit à recopier |

```
kustomize build --load-restrictor=LoadRestrictionsNone \
  deploy/kubernetes/environments/prod-beta/internal/ | kubectl apply -f -
```

## Deux constats consignés, volontairement non corrigés ici

- La `CiliumNetworkPolicy` **n'autorise pas** la passerelle Kevent en sortie, alors que
  `VIDEO_INGEST_KEVENT_GATEWAY_URL` est posée sur les trois charges : le repli ASR Whisper
  ne peut pas aboutir. État du cluster depuis 96 jours, pas une régression de cet ADR.
- `video-ingest-mcp` n'a **aucune sonde** (cf. [ADR-0006](0006-images-epinglees-latest-banni.md)
  pour l'autre moitié du même angle mort).

Les mêler à la remise sous git aurait empêché de prouver, par `kubectl diff`, que le
manifeste décrit bien ce qui tourne. Ils sont à trancher à part.

Voir aussi [ADR-0004](0004-build-in-cluster-buildkit.md) et [ADR-0006](0006-images-epinglees-latest-banni.md).
