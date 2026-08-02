# ADR-0004 — Build in-cluster avec BuildKit rootless (fin du build sur VM)

## Statut
Accepté — 2026-08-02

## Contexte

Le monorepo n'avait qu'une voie de construction d'image : `deploy/scripts/commit-push-build.sh`, qui pousse la branche sur GitHub puis ouvre une session SSH vers la VM cloud `openrag-01-et` pour y lancer `docker buildx`. Cette voie fonctionne, mais elle porte quatre coûts qui se sont accumulés :

1. **Une VM à maintenir hors du cluster.** La VM sert aussi de banc GPU (diarisation vm-direct, cf. `docs/DIARIZATION_BACKEND.md`). Un build qui sature son disque ou son démon Docker dégrade un service de production — deux responsabilités sans rapport sur la même machine.
2. **`SCW_SECRET_KEY` transite par le poste à chaque build.** Le script la lit dans l'environnement local et la pousse par stdin dans le SSH. C'est déjà mieux qu'un argv, mais la clé traverse un poste dont l'environnement est notoirement pollué par VSCode (une clé périmée héritée de l'IDE a déjà fait échouer un `docker login`).
3. **Un maillon SSH.** Toute la chaîne dépend d'un accès `root@` à une IP publique, avec les faux positifs classiques (un reset SSH rend `exit 0`, d'où l'obligation de toujours relire la fin des logs pour savoir si le build a vraiment réussi).
4. **Le build ne part pas d'une source garantie.** Le script distant fait `git reset --hard origin/$BRANCH`, mais rien ne vérifiait côté local que le HEAD poussé correspondait à ce qu'on croyait construire.

La bascule vers un build dans le cluster Scaleway lève les quatre. Reste à choisir le moteur.

## Options considérées

### Option A — Kaniko

Le réflexe historique pour construire une image dans un pod sans démon Docker.

- **Contre, dirimant** : `GoogleContainerTools/kaniko` est **archivé depuis 2025**. Plus aucun correctif, y compris de sécurité, et l'image `executor` n'est plus reconstruite. Introduire une dépendance morte dans une base qu'on doit porter des années est exclu.
- Trois limites secondaires : moteur différent de celui des releases (images seulement *supposées* équivalentes), pas de manifeste multi-architectures, exécution en root.

### Option B — BuildKit rootless

`moby/buildkit`, invoqué par `buildctl-daemonless.sh` dans un Job Kubernetes.

- **Pour** : c'est **le moteur de `docker buildx`**, donc exactement celui qui tournait déjà sur la VM. Une seule implémentation de build : l'équivalence des images cesse d'être une hypothèse. Activement maintenu, mode rootless, multi-arch natif, cache de couches partageable au registre.
- **Contre** : impose `seccomp` et `AppArmor` en `Unconfined` (BuildKit imbrique des espaces de noms utilisateur, que les profils par défaut interdisent). Le multi-arch reste conditionné à QEMU/binfmt sur les nœuds.

### Option C — Buildah

`containers/buildah`, maintenu et orienté rootless.

- **Contre** : introduit une **seconde chaîne d'outils** sans bénéfice ici. Pertinent dans un écosystème podman/RHEL ; le nôtre est docker/buildx.

## Décision

**Option retenue : B — BuildKit rootless**, en Job Kubernetes sur le cluster `internal-gw` (namespace `audio-internal`).

Le critère décisif n'est pas la maintenance — Buildah est maintenu autant — mais **l'unicité du moteur** : `buildx` étant BuildKit, la bascule ne change pas ce qui est produit, seulement où c'est produit. C'est la propriété qui rend le changement sûr à faire d'un coup, sans période de double build.

Le même choix a été fait et validé en production sur `device-management` (ADR-0005 de ce dépôt, 2026-07-26), où l'image in-cluster est tombée à 0,8 Mio de celle produite par buildx.

Ce qu'on accepte de payer : `seccomp`/`AppArmor` en `Unconfined` sur le pod de build. C'est strictement moins que le root exigé par Kaniko, et le pod ne porte aucun jeton de ServiceAccount (`automountServiceAccountToken: false`) — BuildKit n'appelle pas l'API du cluster.

## Conséquences

**Positives**

- Plus de VM ni de SSH dans la chaîne de build ; `openrag-01-et` redevient un banc GPU et rien d'autre.
- `SCW_SECRET_KEY` ne transite plus par le poste à chaque build : elle vit dans un secret `dockerconfigjson` du namespace, posé une fois.
- Le push part du cluster (bande passante interne SCW), pas d'un lien domestique.
- Une garde explicite refuse de lancer si le HEAD local n'est pas sur `origin` : **ce qui est construit est ce qui est poussé**, plus de doute sur la source.
- Le cache de couches est partageable au registre (`--cache`).
- Un build devient possible depuis n'importe quel poste ou runner n'ayant qu'un kubeconfig.

**Négatives / à surveiller**

- `seccomp`/`AppArmor` `Unconfined` requis : à rouvrir si une politique d'admission PodSecurity `restricted` est introduite sur le cluster.
- **Le multi-arch in-cluster n'est pas acquis.** Construire une architecture étrangère aux nœuds exige QEMU/binfmt enregistré (DaemonSet `tonistiigi/binfmt`). Les nœuds Kapsule sont en amd64, seule cible utile en prod-bêta : le défaut est `linux/amd64`. L'arm64 (Docker Desktop sur Mac) reste le domaine d'un build local.
- **Le pod de build porte un droit d'écriture au registre.** Le secret doit idéalement provenir d'une application IAM Scaleway dédiée, ne portant que `ContainerRegistryFullAccess`, plutôt que d'une clé large réutilisée (S3, Kapsule…). C'est le prolongement direct de la segmentation IAM déjà en place côté S3 (writer/reader/cert-mgr).

## Mise en œuvre

| Fichier | Rôle |
|---|---|
| [`deploy/kubernetes/buildkit/buildkit-build-job.yaml`](../../deploy/kubernetes/buildkit/buildkit-build-job.yaml) | Gabarit du Job (jetons `__XXX__`) |
| [`deploy/scripts/build-incluster.sh`](../../deploy/scripts/build-incluster.sh) | Pilote : substitution, garde git, apply, suivi des logs |
| [`deploy/scripts/create-registry-push-secret.sh`](../../deploy/scripts/create-registry-push-secret.sh) | Pose le secret `dockerconfigjson` de push |
| [`deploy/scripts/commit-push-build.sh`](../../deploy/scripts/commit-push-build.sh) | Inchangé en surface : commit + push, mais l'étape build appelle désormais `build-incluster.sh` |

`deploy/scripts/build-push-scw.sh` (buildx local) est **conservé** : il reste la voie de secours si le cluster est indisponible, et la seule voie multi-arch tant que binfmt n'est pas en place.

`REMOTE_HOST` devient optionnel dans `commit-push-build.sh` : s'il est défini, les zones gitignored sont encore rsyncées vers la VM (utile à qui applique les overlays kustomize depuis elle) ; sinon l'étape est sautée proprement. Le build n'en a pas besoin — le `Dockerfile` ne copie que des chemins suivis par git, ce qui est la condition pour qu'un contexte git fonctionne.

## Suivi

- [x] Gabarit + pilote + script de secret écrits, syntaxe vérifiée
- [x] Manifeste validé côté serveur (`kubectl apply --dry-run=server`, k8s 1.35.3)
- [ ] Secret de push posé dans `audio-internal` (nécessite une décision sur la clé IAM à utiliser)
- [ ] Premier build réel validé, image relue dans le registre
- [ ] binfmt sur les nœuds, si le multi-arch in-cluster devient nécessaire

Voir aussi ADR-0005 de `device-management` (même décision, même moteur, validée en production) et [ADR-0003](0003-meeting-source-connectors.md).
