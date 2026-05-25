# Revue sécurité — `video-ingest` V1

> Auto-revue rédigée en autonomie. **À faire valider par un humain** avant ouverture au-delà de la prod-bêta interne.

---

## 1. Modèle de menace résumé

`video-ingest` est un service backend appelé par :
- **Clients front (Mes Réunions)** via le proxy `mesreunions-web` qui forward le JWT OIDC de l'utilisateur.
- **Agents MCP** (Open WebUI, etc.) qui appellent les 5 tools sur le transport `streamable-http`.
- **Admin** pour les purges (rôle Keycloak `video-ingest-admin` ou `admin`).

Surfaces exposées :
- REST `:8000` (auth Bearer obligatoire sauf `/health`).
- MCP `:8001` (**pas d'auth applicative** — repose sur l'absence de Service K8s = non routé hors-pod).
- Worker (pas d'écoute réseau, juste poll/listen Postgres).

---

## 2. Constats par module

### 2.1 `auth.py` — Vérification JWT

| Item | État | Note |
|---|---|---|
| Algorithmes autorisés | ✅ `RS256/384/512` + `ES256/384` uniquement | Pas de `HS256` → pas de confusion attack si JWKS contient une clé symétrique injectée. |
| Validation signature | ✅ via authlib `JsonWebToken.decode(token, keys)` | |
| Validation `exp` / `iat` / `nbf` | ✅ via `claims.validate()` | Automatique côté authlib. |
| Validation `aud` | ⚠️ optionnelle (env `VIDEO_INGEST_OIDC_AUDIENCE`) | V1 cross-clients = volontaire. À renseigner en V1.5 quand l'audience est stabilisée. |
| Validation `iss` | ✅ ajoutée 2026-05-25 (env `VIDEO_INGEST_OIDC_ISSUER`) | Défense en profondeur. |
| JWKS cache | ✅ TTL 1h + refresh-on-KID-miss | Pas de DoS par re-fetch en boucle (KID inconnu refresh **une seule** fois par requête). |
| Bypass DEV | ✅ env `VIDEO_INGEST_AUTH_DISABLED=1` | Logué WARN. **Ne PAS poser en prod**. |
| Rôle admin | ✅ `realm_access.roles` contient `video-ingest-admin` ou `admin` | Pattern Keycloak standard. |

**À renforcer en V1.5** :
- Poser `VIDEO_INGEST_OIDC_AUDIENCE` une fois le client Keycloak dédié créé (aujourd'hui le JWT est issu de `mes-reunions` mais consommé par 2 services).
- Poser `VIDEO_INGEST_OIDC_ISSUER` au déploiement (`https://sso.mirai.fake-domain.name/realms/mirai`).

### 2.2 `api.py` — Endpoints REST

| Item | État | Note |
|---|---|---|
| SQL injection | ✅ toutes les requêtes paramétrées (psycopg2 `%s`) | Pas de f-string SQL nulle part. |
| `_route_url` rejette les URLs hors providers | ✅ HTTP 400 si match=False | |
| `get_job` ne fuite pas l'existence cross-user | ✅ 404 si owner mismatch (pas 403) | |
| `/search` injection tsquery | ✅ `plainto_tsquery` neutralise les opérateurs | Pas de prepared `to_tsquery` brut. |
| `/transcript?format=markdown` rendering | ✅ JSON-encoded, le rendu HTML est de la responsabilité du client | |
| `DELETE` admin | ✅ `require_admin` + audit | Cascade gérée par les FK `ON DELETE CASCADE`. |
| Body size | ⚠️ pas de limite explicite Flask | Le reverse proxy K8s impose une borne. Acceptable mais à documenter. |

### 2.3 `mcp_server.py` — Surface MCP

| Item | État | Note |
|---|---|---|
| Auth applicative | ❌ **aucune** | Le tool `video_import` prend `user_sub` en paramètre. C'est documenté : le client MCP est trusted. |
| Protection réseau | ✅ pas de Service K8s exposant `:8001` | Le pod écoute mais rien ne route vers lui depuis l'extérieur. Si on expose un jour, il faut **rajouter une auth Bearer** identique à l'API REST. |
| `video_purge` admin token | ⚠️ shared secret via env `VIDEO_INGEST_MCP_ADMIN_TOKEN` | OK pour V1, mais à remplacer par un check JWT proprement quand on expose. |

**Action recommandée avant exposition MCP réelle** : ajouter un middleware d'auth côté serveur MCP (FastMCP supporte des hooks `BaseHTTPMiddleware`).

### 2.4 `quotas.py` — Anti-abus

| Item | État | Note |
|---|---|---|
| Race condition check-then-enqueue | ⚠️ acceptée | Deux requêtes parallèles peuvent toutes deux passer le check et dépasser le quota de +1. Pour un compteur anti-abus c'est largement acceptable. |
| HIT cache consomme-t-il du quota ? | ✅ non (volontaire) | Le coût réel est sur le MISS. |

### 2.5 `audit.py` — Traçabilité

| Item | État | Note |
|---|---|---|
| L'écriture audit peut-elle tuer le flux ? | ✅ non — swallow exceptions + log Python | Compromis assumé, documenté. |
| Préserve la trace après purge ? | ✅ `video_source_id` est un pointeur soft (pas de FK) | |

### 2.6 `providers/youtube/audio.py` — Fallback ASR

| Item | État | Note |
|---|---|---|
| Audio NON persisté | ✅ `TemporaryDirectory` détruit à la sortie du `with` | Test E2E `test_fetch_audio_full_pipeline_and_cleans_up` vérifie. |
| Audio en RAM | ⚠️ `audio_path.read_bytes()` charge tout en mémoire | Pour une vidéo 1h ≈ 50-100 MB. À surveiller via metrics. À reworker en streaming si on attaque des vidéos très longues. |
| Mapping vidéo privée/blocked | ✅ → `VideoUnavailable` | Pas de retry inutile côté worker. |

### 2.7 Front modale (`tabs/meetings.js`)

| Item | État | Note |
|---|---|---|
| XSS dans les messages d'erreur | ✅ `textContent` partout (jamais `innerHTML`) | |
| URL utilisateur réfléchie | ✅ envoyée JSON, jamais réinjectée dans le DOM | |
| Mention légale affichée | ✅ Q6 résolue, à valider service juridique | |

---

## 3. Surface réseau totale (récap)

| Composant | Port | Auth | Exposition K8s |
|---|---|---|---|
| `video-ingest-api` | 8000 | JWT Bearer | Service ClusterIP (intra-cluster) |
| `video-ingest-worker` | — | n/a | aucun écoute réseau |
| MCP `streamable-http` | 8001 | **aucune** | aucun Service défini = non routable |
| Egress | 443 | n/a | Cilium FQDN allowlist (mode A) |

---

## 4. Dépendances tierces

| Package | Risque | Action |
|---|---|---|
| `yt-dlp` | ⚠️ casse régulièrement quand YouTube bouge | Bump auto via Dependabot/Renovate + alerte si provider échoue en masse (à câbler en V1.5) |
| `youtube-transcript-api` | ⚠️ idem | Bump auto |
| `authlib` | ✅ stable, large adoption | Suivre les CVE |
| `psycopg2-binary` | ✅ standard | |
| `flask` 3.x + `gunicorn` 23.x | ✅ standard | |
| `mcp` (Anthropic SDK) | ⚠️ jeune, API peut bouger | Pinner la version exacte en V1.5 |

---

## 5. Reste à faire avant ouverture large

1. **Renseigner `VIDEO_INGEST_OIDC_AUDIENCE` et `VIDEO_INGEST_OIDC_ISSUER`** dans l'overlay prod-bêta.
2. **Ne PAS exposer le port 8001 (MCP)** sans rajouter un middleware d'auth.
3. **Configurer Dependabot/Renovate** sur `yt-dlp` + `youtube-transcript-api` (procédure à documenter dans le README).
4. **Cas force_audio** : surveiller la mémoire du worker (gros audios). Limit K8s actuelle = 1Gi → re-évaluer après les premières mesures.
5. **Audit log retention** : décider d'une politique (purge >12 mois ? archivage S3 ?). Pas de purge auto en V1, à voir avec le DPO.
6. **Pen-test léger** : OWASP ZAP en mode passif sur les endpoints + scan dépendances.

---

_Rédigé le 2026-05-25 lors de la session de livraison V1._
