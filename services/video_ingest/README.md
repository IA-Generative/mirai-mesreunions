# `video-ingest` — service mutualisé d'ingestion vidéo (V1 en cours)

> Spec complète : [`features-2-build/FEATURE_video-ingest.md`](../../features-2-build/FEATURE_video-ingest.md).

Composant **autonome et extrayable** : il vit temporairement dans ce
repo (en cours de rename `mirai-mesreunions` → `mirai-mesreunions`)
mais sera transféré dans son propre dépôt dès maturité V1 (cf. décision
D14). Mes Réunions et Mes Collections sont des **clients** qui parlent
au service par API REST/MCP, jamais par import Python.

## Règles d'isolation (à respecter à chaque PR)

1. **Aucun import** depuis `libs.shared.*` ni depuis un autre `services/*`.
   Les utilitaires nécessaires sont recopiés ici (au pire dupliqués),
   pas mutualisés via le code existant.
2. **Aucune FK** vers les tables MirAI (`user_audio_files`, `meetings`,
   `briefs`, …). Toutes les tables du service sont préfixées `video_*`
   et l'identité utilisateur est un `user_sub` opaque (sub OIDC).
3. **Schéma BDD séparable** : aujourd'hui dans `migrations/internal/019_*`
   pour cohabiter avec le reste, mais regroupable d'un bloc sans devoir
   trier ligne par ligne.
4. **Interface = REST + MCP uniquement**. Pas de signal, pas de bus
   partagé, pas de table commune.
5. **Pas de nouvelle dépendance d'orchestration** : la file de jobs est
   en Postgres natif (`SELECT … FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY`),
   cf. D13.

**Critère de sortie** : `git filter-repo --path services/video_ingest/ --path migrations/internal/019_video_ingest_initial.sql --path tests/unit/test_video_ingest_*.py` doit produire un repo viable, sans référence dangling au reste.

## Nom du package Python

Le dossier est `services/video_ingest/` (underscore) — exception
assumée par rapport au reste du repo (où les services sont en
`kebab-case`) pour rester importable directement en Python sans
gymnastique `importlib`, et pour préfigurer le futur package
`video_ingest` dans son propre repo.

## Egress internet (Q7 / D15)

Deux modes, **bascule config-only**, aucun code à modifier entre les deux.

### Mode A — egress direct (par défaut, prod-bêta interne)

Le pod parle directement à YouTube. Une `CiliumNetworkPolicy` FQDN-aware
autorise uniquement les hôtes nécessaires :

```yaml
egress:
  - toFQDNs:
      - matchPattern: "*.youtube.com"
      - matchPattern: "*.googlevideo.com"   # CDN audio/vidéo
      - matchPattern: "*.ytimg.com"         # thumbnails
    toPorts:
      - ports: [{ port: "443", protocol: TCP }]
```

Pas de variable d'environnement particulière à poser : si `HTTP_PROXY`
et `HTTPS_PROXY` sont absentes, yt-dlp et `youtube-transcript-api`
appellent en direct.

### Mode B — via proxy rotatif (environnement souverain)

Tout l'egress passe par le **proxy rotatif existant déployé par
`owuicore-main/infra/proxy/`** (HAProxy + 4 VMs Squid sur Scaleway,
20 IPs rotatives, Basic Auth). Aucun nouveau composant à monter — on
réutilise ce qui sert déjà à SearXNG et websnap.

Service K8s côté `owuicore-main` : `rotating-proxy.miraiku.svc:3128`
(cluster `brave-bassi`). Depuis un autre cluster, utiliser l'IP publique
HAProxy avec Basic Auth (cf. README du proxy là-bas).

Recette de bascule :

1. Récupérer la clé API auprès du mainteneur d'`owuicore-main` (gérée hors-Git).
2. Poser le secret K8s :
   ```bash
   kubectl create secret generic video-ingest-proxy \
     --from-literal=url='http://owui:<API_KEY>@rotating-proxy.miraiku.svc:3128'
   ```
3. Patcher le Deployment `video-ingest` (via overlay kustomize) :
   ```yaml
   env:
     - name: HTTP_PROXY
       valueFrom: { secretKeyRef: { name: video-ingest-proxy, key: url } }
     - name: HTTPS_PROXY
       valueFrom: { secretKeyRef: { name: video-ingest-proxy, key: url } }
     - name: NO_PROXY
       value: "localhost,127.0.0.1,.svc,.cluster.local"
   ```
4. Durcir la `CiliumNetworkPolicy` : retirer la section `toFQDNs` YouTube,
   ne laisser que l'egress vers `rotating-proxy` (port 3128).

**Règle de codage permanente** : aucun appel HTTP custom dans le code de
`video-ingest`. Toujours passer par `yt-dlp`, `youtube-transcript-api`,
ou `requests` (avec `trust_env=True`, qui est le défaut) — toutes ces
libs respectent `HTTP_PROXY`/`HTTPS_PROXY` automatiquement. Tout PR qui
introduit `httpx.Client(proxy=None)` ou `urllib.request` sans respect de
l'env doit être refusé.

## État courant

| Brique | Statut |
|---|---|
| Schéma BDD initial (migration 019) | ✅ posé |
| Parseur URL YouTube (dédup) | ✅ posé + tests |
| Provider YouTube (metadata + sous-titres + audio) | ⏳ à faire |
| Worker Postgres-native | ⏳ à faire |
| API REST `/import`, `/transcripts/{id}`, `/search` | ⏳ à faire |
| Outils MCP V1 (5 tools) | ⏳ à faire |
| Intégration client Mes Réunions | ⏳ à faire |

Voir le journal d'itération dans la spec pour le détail.
