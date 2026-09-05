# Runbook — déployer sur prod-bêta sans rien casser

Écrit le 2026-08-30, après une journée où l'on a découvert que trois charges tournaient
sans manifeste et que la base restante, appliquée telle quelle, aurait éteint la
transcription. Tous les chiffres cités ont été relevés ce jour-là.

Public : qui doit corriger, livrer ou reprendre une charge de `audio-internal`
(cluster `internal-gw`).

> **R1 — aucune écriture sur `external-gw`.** La zone exposée est en lecture seule depuis
> ce dépôt. Tout ce qui suit concerne `internal-gw`.

---

## 1. Avant tout geste : cette charge a-t-elle une source ?

C'est la première question, et elle a une mauvaise réponse une fois sur quatre.

| Charge (`audio-internal`) | Source du manifeste |
|---|---|
| `video-ingest-api`, `video-ingest-mcp`, `video-ingest-worker` | ✅ `deploy/kubernetes/environments/prod-beta/internal/` — versionné |
| `device-token-authority`, `internal-ingester`, `transcription-relay` | ⚠ `internal-zone/deployments.yaml`, hors dépôt **et fortement dérivé** (§3) |
| `mesreunions-web`, `admin-console` | ⛔ **aucune** |

**Piège** : `external-zone/deployments.yaml` contient des entrées nommées `mesreunions-web`
et `admin-console`, mais en namespace **`audio-external`** — l'autre cluster. Chercher le
nom seul fait croire qu'elles sont couvertes. **Toujours vérifier le namespace.**

Si la charge n'a pas de source, **la reconstruire d'abord** (§5). Sinon le correctif
disparaîtra au prochain `apply`, sans un mot.

---

## 2. Livrer un changement de code

```bash
# 1. POUSSER D'ABORD — le build part de git, pas de la copie de travail.
git push origin <branche>

# 2. Construire, tag immuable, SANS latest (cf. ADR-0006).
export REGISTRY_NAMESPACE=funcscwnspricelessmontalcinhiacgnzi
deploy/scripts/build-incluster.sh "$(date +%Y%m%d-%H%M%S)" --no-latest

# 3. Poser le tag DANS le manifeste, puis appliquer.
#    (charges versionnées seulement — sinon voir §4)
kustomize build --load-restrictor=LoadRestrictionsNone \
  deploy/kubernetes/environments/prod-beta/internal/ | kubectl apply -f -
```

`build-incluster.sh` refuse un commit absent d'`origin` : c'est voulu, il construit la réf
distante. Le contourner avec `--allow-unpushed` produit une image que personne ne pourra
reconstruire.

**Ne faire rouler que ce qui doit rouler.** L'image est celle du monorepo : elle contient
tout le dépôt à ce commit. Basculer une charge saine sur une image construite depuis une
branche de travail y expédie ce travail. Vérifier ce qui change réellement pour le
composant visé :

```bash
git log main..HEAD -- services/<composant>/ requirements.txt   # vide = composant identique
```

---

## 3. ⛔ Ne jamais appliquer `internal-zone/deployments.yaml`

C'est une **base minimale volontairement inerte**, pour qu'un développeur puisse la copier
sans casser son poste. La vraie configuration de prod-bêta était portée par un overlay
kustomize — **qui n'existe plus** : `git log --all` prouve qu'il n'a jamais été committé, et
la machine de construction ne l'a pas non plus.

**Les valeurs de prod-bêta ne survivent donc que dans les pods en cours d'exécution.**

Mesuré le 2026-08-30 (`kubectl diff`), un `apply` de cette base ferait, sur
`internal-ingester` :

| Variable | Cluster | Base — ce qu'un apply imposerait |
|---|---|---|
| `TRANSCRIPTION_BACKEND` | `kevent` | **`stub`** — plus aucune transcription réelle |
| les 7 `KEVENT_*_ENABLED` | `true` | **`false`** |
| `KEVENT_GATEWAY_URL`, `LITELLM_BASE_URL`, `MCR_GATEWAY_URL`, `OIDC_TOKEN_ENDPOINT` | renseignés | **vides** |
| `LLM_HTTP_TIMEOUT_SECONDS` | posée | **supprimée** |
| `RABBITMQ_HOST` | `10.10.0.11` | un nom DNS qui ne résout pas d'ici |
| `EXTERNAL_CALLBACK_URL` | l'hôte public du dépôt | une adresse `cluster.local` |

Et sur `device-token-authority` : `DEVICE_TOKEN_RETENTION_HOURS` `360` → **`168`** (7 jours
au lieu de 15 — la rétention côté terminal est la source de vérité, la raccourcir périme
des enregistrements que les utilisateurs croient conservés).

C'est déjà arrivé le 2026-05-17. À l'époque, rejouer l'overlay réparait. **Ce recours
n'existe plus.**

### Lire un `kubectl diff` sans se tromper

Beaucoup de lignes `+`/`-` ne sont que du **réordonnancement** : le même nom apparaît des
deux côtés avec la même valeur. Comparer les **valeurs**, pas les noms :

```bash
kubectl diff -f <fichier> | grep -E '^[+-]' | grep -v last-applied
```

Si le diff ne montre que des déplacements, l'`apply` reste un rollout inutile — mais pas une
perte. S'il montre une valeur différente, s'arrêter.

---

## 4. Changement ciblé sur une charge sans manifeste fiable

Chirurgical, ne touche rien d'autre :

```bash
kubectl -n audio-internal set image deploy/<nom> <conteneur>=<image>:<tag>
kubectl -n audio-internal set env  deploy/<nom> CLE=valeur
kubectl -n audio-internal rollout status deploy/<nom> --timeout=180s
```

Le nom du conteneur n'est pas toujours celui du déploiement :

```bash
kubectl -n audio-internal get deploy <nom> -o jsonpath='{.spec.template.spec.containers[0].name}'
```

C'est un **pis-aller** : le changement n'a pas de source. Le tracer, et ouvrir la
reconstruction du manifeste (§5).

---

## 5. Reconstruire un manifeste perdu

Tant que la charge tourne, sa description survit dans une annotation. C'est la seule copie —
si le déploiement est supprimé, elle part avec lui.

```bash
kubectl -n audio-internal get deploy <nom> \
  -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}' \
  | python3 -m json.tool
```

Ne pas oublier ce qui gravite autour : `kubectl -n audio-internal get svc,cnp,scaledobject`.

Ensuite :

1. Réécrire en YAML lisible, commenté — **pourquoi**, pas seulement quoi.
2. **Expurger les hôtes réels** (dépôt public) : convention `*.fake-domain.name` /
   `example.com`, valeurs réelles dans un `*.local.yaml` gitignoré que la kustomization
   **exige** (cf. [ADR-0005](adr/0005-manifestes-versionnes-hotes-expurges.md)).
3. Ordonner les variables patchées **en tête** : la fusion kustomize les y remonte, et
   aligner la base rend les `diff` suivants vides.
4. **Prouver** :

```bash
kustomize build --load-restrictor=LoadRestrictionsNone . | kubectl diff -f - ; echo "exit=$?"
```

**`exit=0` est le critère d'acceptation.** Un manifeste qui ne décrit pas exactement ce qui
tourne n'est pas une source, c'est une opinion.

5. `gitleaks` avant de committer, et relire qu'aucun hôte réel n'entre dans un fichier suivi.

---

## 6. Contrôles de fin

```bash
# Aucune image latest (ADR-0006)
kubectl -n audio-internal get deploy \
  -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].image}{"\n"}{end}' | grep -c ':latest'   # attendu 0

# Répliques au complet
kubectl -n audio-internal get deploy \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,VOULU:.spec.replicas'

# Redémarrages (une charge qui « tourne » peut boucler)
kubectl -n audio-internal get pods \
  -o custom-columns='NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount'
```

Puis, côté supervision de la bêta (dépôt `mirai-apps-beta-private`) :
`scripts/verify-all.sh`. La carte « Mes réunions » de `/etat` agrège les répliques **par
namespace** : une charge `video-ingest` à terre suffit à la faire passer *dégradée*, et
c'est voulu — c'est le signal qui a fait découvrir la panne du 2026-08-30.

---

## 7. Pièges déjà payés

- **Une charge sans sonde ne dit pas qu'elle est morte.** `video-ingest-mcp` n'a ni
  `livenessProbe` ni `readinessProbe` : il bind `0.0.0.0:8001` dans le constructeur ; s'il
  retombait sur le défaut loopback, le pod resterait « Running » et injoignable.
- **Une dépendance sans borne haute finit par sortir en majeure.** `mcp>=1.2.0` a tiré 2.x
  et tué le serveur MCP (68 redémarrages en 5 h 27). Toute ligne `>=` sans plafond est un
  incident à retardement — le fichier concerné expliquait pourtant six lignes plus haut
  pourquoi `yt-dlp` était épinglé exact.
- **Un objet KEDA orphelin bruite indéfiniment.** `transcription-stub-scaledobject` visait un
  Deployment disparu au rebranding du 2026-05-16 : 188 lignes d'erreur en 8 h dans le ns
  `keda`. Avant de supprimer, prouver que rien ne le recrée (`grep` dans le dépôt **et** sur
  la machine de construction) et sauvegarder l'objet.
- **Une ConfigMap montée `optional: true` qui manque ne casse rien** — elle dégrade en
  silence. Contrôler l'empreinte **vue par le pod**, pas la présence de la ConfigMap.

## Voir aussi

- [ADR-0005](adr/0005-manifestes-versionnes-hotes-expurges.md) — manifestes versionnés, hôtes expurgés
- [ADR-0006](adr/0006-images-epinglees-latest-banni.md) — tags immuables, `latest` banni
- [ADR-0004](adr/0004-build-in-cluster-buildkit.md) — build in-cluster
- [`deploy/kubernetes/environments/README.md`](../deploy/kubernetes/environments/README.md)
- [`RUNBOOK_PIPELINE_RELIABILITY.md`](RUNBOOK_PIPELINE_RELIABILITY.md)
