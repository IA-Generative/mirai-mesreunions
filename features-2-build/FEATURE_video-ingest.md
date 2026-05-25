# Feature — `video-ingest` : ingestion mutualisée de vidéos web

> **Document vivant.** Mémoire de cycle de vie SAFe de la feature, lue et mise à jour par le coding assistant à chaque itération. Tenir à jour les sections « État courant », « Décisions », « Journal d'itération » et « Questions ouvertes » à chaque PR.

---

## 1. Métadonnées feature

| Champ | Valeur |
|---|---|
| **Nom** | `video-ingest` |
| **Propriétaire métier** | Éric (SDID / DTNUM) |
| **Branche Git principale** | `feature/youtube-import` |
| **Statut SAFe** | À initier — non encore positionnée dans un PI |
| **Niveau** | Feature (composant transverse réutilisable) |
| **Dernière mise à jour** | 2026-05-25 — V1 **complète** : backend + frontend modale + revue sécu auto. 126 tests verts. Reste activation prod-bêta humaine. |

---

## 2. Énoncé métier (Feature Statement)

**En tant qu'** agent du ministère utilisateur de MirAI,
**je veux** importer une vidéo YouTube (puis Dailymotion) à partir de son URL ou d'une liste d'URLs,
**afin de** disposer immédiatement de sa transcription comme objet métier interrogeable (réunion, collection, RAG à terme),
**sans** redéclencher de travail de transcription si un autre agent a déjà importé la même vidéo.

### Bénéfices attendus
- **Cache mutualisé efficace** : une vidéo n'est transcrite qu'une seule fois pour toute l'instance.
- **Sobriété** : aucun stockage de média audio/vidéo, lecture depuis la source d'origine.
- **Réutilisabilité** : composant exposé en MCP, consommable par Mes Réunions, Mes Collections, et tout futur module MirAI.
- **Trajectoire RAG** : structuration dès V1 pour permettre une intégration ultérieure dans le RAG MirAI sans refonte.

---

## 3. Principes directeurs (non négociables)

Ces principes encadrent toute décision d'implémentation. Le coding assistant les consulte avant tout arbitrage technique.

1. **Sous-titres d'abord.** Priorité absolue aux sous-titres YouTube (manuels puis auto-générés en français). Whisper local n'est qu'un fallback explicite (flag `force_audio`).
2. **Pas de stockage de média.** Aucun fichier audio ou vidéo n'est persisté après transcription. La vidéo se joue depuis YouTube (iframe ou lien horodaté).
3. **Composant autonome — service mutualisé destiné à être extrait du monorepo.** `video-ingest` est un module à part, avec sa propre persistance, sa propre interface MCP/REST, et son propre cycle de vie. Mes Réunions et Mes Collections sont des **clients** (par API uniquement), pas des hôtes. **Conséquence directe sur V1** : aucun import Python croisé avec le reste du repo, aucun partage de schéma BDD (tables préfixées `video_*` dans un schéma dédié ou base séparable), pas de FK vers les tables MirAI, identité utilisateur = `user_sub` opaque (pas de jointure). Objectif : un `git filter-repo` ou un simple `cp -r services/video-ingest/` doit suffire à extraire le composant dans son propre repo le jour venu.
4. **Dédup au niveau ressource.** L'unicité d'une vidéo est `(provider, provider_video_id)` après normalisation d'URL, pas l'URL brute.
5. **Séparation ressource partagée / appropriation utilisateur.** `VideoSource` et `Transcript` sont partagés. `UserVideoBookmark` matérialise l'usage par utilisateur/contexte.
6. **Visibilité des données : décision différée.** Tout est conservé sans filtrage côté backend en V1. L'arbitrage visibilité se fera à l'introduction du RAG, par filtrage au query time (pas par fragmentation des index).
7. **Pas de RAG en V1.** Recherche full-text PostgreSQL (`tsvector` français) suffisante au volume attendu. Le RAG, quand il viendra, vivra dans MirAI, pas dans `video-ingest`.
8. **Chunking temporel.** Les transcripts sont segmentés en fenêtres temporelles (60-90s, chevauchement 15s) avec `start_seconds` / `end_seconds`, pour permettre le lien horodaté vers la source.

---

## 4. Architecture cible

### 4.1 Modèle de données

```
VideoSource (partagé, une ligne par vidéo unique)
├── id (PK)
├── provider (enum: youtube, dailymotion, …)
├── provider_video_id
├── canonical_url
├── title
├── channel
├── duration_sec
├── published_at
├── fetched_at
├── metadata_json
└── UNIQUE (provider, provider_video_id)

Transcript (partagé, attaché à VideoSource)
├── id (PK)
├── video_source_id (FK)
├── language (fr, en, …)
├── method (subtitle_manual, subtitle_auto, asr_whisper_v3, …)
├── content_text (texte intégral, post-traité)
├── content_text_raw (avant post-traitement LLM)
├── segments_json (chunks temporels avec start_seconds/end_seconds)
├── created_at
└── INDEX full-text (tsvector français) sur content_text

UserVideoBookmark (par utilisateur)
├── id (PK)
├── user_id
├── video_source_id (FK)
├── bookmarked_at
├── context (meeting, collection, …)
├── context_id
└── user_notes
```

### 4.2 Interface provider (abstraction multi-source)

```python
class VideoProvider:
    name: str  # "youtube", "dailymotion"
    def matches_url(url) -> bool
    def parse_canonical_id(url) -> str           # critique pour la dédup
    def fetch_metadata(canonical_id) -> VideoMetadata
    def fetch_subtitles(canonical_id, langs) -> Transcript | None
    def fetch_audio(canonical_id) -> Path        # fallback ASR uniquement
```

V1 : `YouTubeProvider` (yt-dlp + youtube-transcript-api).
V2 : `DailymotionProvider`.

### 4.3 Interface MCP exposée

| Tool MCP | Description | V |
|---|---|---|
| `video.import(url, language?, force_audio?, context?)` | Déclenche ingestion. Renvoie `{video_source_id, status, reused: bool}` | V1 |
| `video.get_transcript(id_or_url, language?, format?)` | Renvoie le transcript (texte / segmenté / markdown) | V1 |
| `video.get_metadata(id_or_url)` | Métadonnées seules | V1 |
| `video.search(query, filters?)` | Recherche full-text PostgreSQL | V1 |
| `video.purge(video_source_id)` | Purge technique (admin) | V1 |
| `video.refetch(video_source_id)` | Re-transcription (admin) | V1.5 |
| `video.rag_query(question, filters?)` | Proxy vers RAG MirAI | V2 |

### 4.4 Flux d'ingestion (job worker par URL)

```
1. Normaliser URL → (provider, provider_video_id)
2. Lookup VideoSource existant
   ├── HIT  → reused=true, créer UserVideoBookmark, terminer
   └── MISS → continuer
3. fetch_metadata via provider → créer VideoSource, MAJ titre
4. Statut UI: youtube_fetching ("Récupération des sous-titres : {titre}")
5. SI force_audio=false : tenter fetch_subtitles(langs=[fr, en])
   └── succès → créer Transcript(method=subtitle_*), passer à 7
6. Fallback ASR : fetch_audio → Whisper large-v3 → créer Transcript(method=asr_*)
   └── audio NON persisté après transcription
7. Post-traitement LLM (V1.5) : repunctuation + correction jargon → content_text
8. Chunking temporel → segments_json
9. Indexation tsvector
10. Créer UserVideoBookmark, statut final ready
```

### 4.5 Intégration côté clients

**Mes Réunions (V1)**
- Bouton « Importer depuis YouTube » → modale (URL unique / liste / upload .txt, langue, toggle force_audio)
- Appel REST → `video-ingest` → `{video_source_id, reused}`
- Création d'un `UserVideoBookmark(context=meeting)` et d'un `Meeting` lié
- Nouveau statut UI : `youtube_fetching` — libellé « Récupération de l'audio ou des sous-titres de la vidéo : {titre} »
- Titre affiché : nom de la vidéo (métadonnées). Identifiant interne : URL canonique.
- Si `reused=true` : statut `ready` immédiat.

**Mes Collections (V2)**
- Bouton « Ajouter une vidéo » sur une collection → même API → `UserVideoBookmark(context=collection)`.

---

## 5. Plan de livraison (incréments SAFe)

### V1 — MVP partagé (objectif PI courant)
- [x] Schéma BDD `VideoSource` / `Transcript` / `UserVideoBookmark` _(+ `video_ingest_jobs` pour la file Postgres-native)_
- [x] `YouTubeProvider` (yt-dlp + youtube-transcript-api) _— metadata + sous-titres (manuels prioritaires sur auto). Fallback audio (force_audio) reste à coder dans la slice ASR._
- [x] Normalisation URL YouTube (formats `youtube.com/watch`, `youtu.be`, `shorts`, `embed`, `live`, `&t=`, paramètres parasites ; playlists et channels rejetés)
- [x] Pipeline d'ingestion async — worker Postgres-native (D13) : claim `FOR UPDATE SKIP LOCKED`, `LISTEN/NOTIFY` + poll fallback 5s, heartbeat 30s, watchdog 60s reprise orphelins
- [x] Fallback Whisper sous flag `force_audio` _(chemin Kevent retenu : `providers/youtube/audio.py` + `_kevent.py` mini-client autonome D14)_
- [x] **Aucun stockage audio post-transcription** _(TemporaryDirectory + test E2E `test_fetch_audio_full_pipeline_and_cleans_up` qui vérifie l'absence du dossier après retour)_
- [x] Index full-text `tsvector('french', content_text)` _(colonne GENERATED ALWAYS AS … STORED + index GIN + endpoint /search avec ts_rank + ts_headline)_
- [x] Endpoints REST internes + 5 outils MCP V1 _(7 endpoints REST + 5 tools MCP via FastMCP transport streamable-http)_
- [x] Intégration Mes Réunions : backend (blueprint `youtube_import` + migration 020 + access_token en session) + **frontend** (bouton 🎬 YouTube + modale `<dialog>` autonome avec mention légale + polling job_id + gestion 429 quota)
- [x] Endpoint `video.purge` (admin) _(DELETE /video/sources/<id> + tool MCP video_purge avec admin_token)_
- [ ] Tests unit (parsing URL, dédup, sanitization) + intégration (mocks providers) + E2E manuel documenté

### V1.5 — Qualité
- [ ] Post-traitement LLM (Mistral Small) : repunctuation + correction jargon, sur `content_text_raw` → `content_text`
- [ ] Lecteur vidéo IHM avec lien horodaté depuis extrait de transcript (`?t=Ys`)
- [ ] `video.refetch` admin
- [ ] Détection de doublon à l'import (UX : signaler au lieu de re-créer un bookmark)

### V2 — Extension + RAG
- [ ] `DailymotionProvider`
- [ ] Intégration RAG MirAI : transcripts exposés comme collection MirAI
- [ ] Embedding aligné sur le stack MirAI (cohérence cross-corpus)
- [ ] `video.rag_query` (proxy MCP vers MirAI)
- [ ] Intégration Mes Collections
- [ ] Arbitrage visibilité (ACL au query time)

---

## 6. Décisions actées (Decision Log)

| # | Date | Décision | Rationale |
|---|---|---|---|
| D1 | 2026-05-25 | Objet métier de premier rang : URL et provenance persistés | Permet re-fetch, dédup, lien vers source, et anticipe RAG |
| D2 | 2026-05-25 | Mutualisation au niveau `VideoSource` + `Transcript` partagés | Cache efficace : 1 vidéo = 1 transcription, quel que soit le nombre d'utilisateurs |
| D3 | 2026-05-25 | Composant autonome avec interface MCP | Réutilisable par Mes Réunions, Mes Collections, futurs modules |
| D4 | 2026-05-25 | Pas de stockage de média (audio/vidéo) | Sobriété + droits d'auteur + cohérence FDR Numérique Responsable |
| D5 | 2026-05-25 | Sous-titres YouTube prioritaires sur Whisper local | Qualité Google ASR fr suffisante, coût compute nul, mises à jour gratuites |
| D6 | 2026-05-25 | Pas de RAG en V1, recherche full-text PostgreSQL | Volume V1 ne le justifie pas ; éviter dette archi |
| D7 | 2026-05-25 | RAG futur vivra dans MirAI, pas dans `video-ingest` | Cohérence avec stack RAG 4-couches existant ; pas de fragmentation |
| D8 | 2026-05-25 | Chunking par fenêtre temporelle (60-90s, chevauchement 15s) | Permet liens horodatés et limite hallucinations futures RAG |
| D9 | 2026-05-25 | Index embedding unique global, filtrage au query time | Pas de recalcul d'embeddings selon utilisateur ; flexibilité visibilité |
| D10 | 2026-05-25 | Visibilité données : décision différée jusqu'à V2 | Tout est conservé sans filtrage en V1 ; arbitrage au moment de l'intro RAG |
| D11 | 2026-05-25 | Vidéo figée par défaut, `refetch` réservé admin | YouTube ne mute pas le contenu ; éviter refresh sauvage |
| D12 | 2026-05-25 | Provider Dailymotion reporté à V2 | Focaliser V1 sur YouTube, abstraction prête dès V1 |
| D13 | 2026-05-25 | Orchestrateur async = **Postgres natif** (`SELECT … FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY`), **pas de nouvelle dépendance** | Charge V1 modeste (imports manuels), transactionnalité `INSERT VideoSource + enqueue` élimine la classe « purgatoires » (ADR-0001), composant autonome → ne pas le coupler au RabbitMQ MirAI existant. **Veille** : réévaluer si le volume passe à un flux soutenu, ou si l'écosystème (procrastinate, pgmq, river-py…) mûrit suffisamment pour justifier une lib externe |
| D14 | 2026-05-25 | **Mission = service mutualisé MirAI**, hébergé temporairement dans le repo « Mes Réunions » (en cours de rename `mirai-mesreunions` → `mirai-mesreunions`) et destiné à être **extrait dans son propre repo** dès maturité V1 | Le repo hôte devient explicitement « un client parmi d'autres » ; héberger durablement `video-ingest` dedans créerait une dette de couplage. Conséquences V1 : (a) zéro import Python croisé avec le code Mes Réunions, (b) tables préfixées `video_*` dans un schéma dédié, aucune FK vers les tables MirAI, (c) identité = `user_sub` opaque, pas de jointure, (d) interface = REST + MCP uniquement, (e) déployable indépendamment (Dockerfile + manifeste K8s autoporteurs). Critère de sortie : `git filter-repo --path services/video-ingest/` doit produire un repo viable |
| D15 | 2026-05-25 | **Q7 — Egress vers youtube.com : A par défaut, B prête côté code** | **Mode A (prod-bêta interne)** : egress direct du pod via `CiliumNetworkPolicy` FQDN-aware autorisant `*.youtube.com`, `*.googlevideo.com`, `*.ytimg.com`. Pattern déjà éprouvé en CDS N1. **Mode B (cible ministérielle)** : aucun egress direct, tout passe par le **proxy rotatif existant `rotating-proxy.miraiku.svc:3128`** (cluster `brave-bassi`, ns `miraiku`) déjà déployé par `owuicore-main/infra/proxy/` — HAProxy + 4 Squid Scaleway = 20 IPs rotatives, Basic Auth `owui:<API_KEY>`. **Bascule A→B = config-only** : poser `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` dans l'overlay K8s + durcir la NetworkPolicy. Côté code, **rien à implémenter** : yt-dlp et `youtube-transcript-api` honorent ces variables nativement. Exigence permanente sur les futurs PRs : **aucun appel HTTP custom** (toujours passer par les libs qui respectent les env proxy) |

---

## 7. Questions ouvertes (à arbitrer avant ou pendant le dev)

| # | Question | Échéance souhaitée | Réponse |
|---|---|---|---|
| Q1 | Quel orchestrateur de jobs async utiliser (Celery / Temporal / autre déjà en place dans le repo MirAI) ? | Avant début V1 | **Résolu 2026-05-25 (D13)** : Postgres natif, pas de nouvelle dépendance. À réévaluer si volume ou maturité des libs (procrastinate, pgmq) le justifient |
| Q2 | Iframe YouTube standard ou composant maison pour lecteur horodaté ? | V1.5 | _Ouvert_ |
| Q3 | Quel modèle pour post-traitement LLM (Mistral Small via API MirAI ?) ? | V1.5 | _Ouvert_ |
| Q4 | Quotas par utilisateur sur les imports (anti-abus) ? | V1 ou V1.5 | **Résolu 2026-05-25** : `VIDEO_INGEST_QUOTA_IMPORTS_PER_DAY` (défaut 50, 0 = désactivé), fenêtre glissante 24h sur `video_ingest_jobs.created_at`. HIT cache ne consomme pas. HTTP 429 si dépassé |
| Q5 | Logging d'audit : qui a importé quoi quand (pour traçabilité interne) ? | V1 | **Résolu 2026-05-25** : migration 021 + table `video_ingest_audit` (action, user_sub, url, video_source_id soft, reused, job_id, context, details_json). Logged sur import (HIT+MISS), purge, quota dépassé |
| Q6 | Mention légale dans la modale d'import (responsabilité droits) ? | V1 | _Ouvert — recommandé_ |
| Q7 | Sortie internet vers youtube.com : passerelle/proxy à configurer en environnement souverain ? | V1 | **Résolu 2026-05-25 (D15)** : A par défaut (egress direct + Cilium FQDN allowlist en prod-bêta), B prête (proxy rotatif `rotating-proxy.miraiku.svc:3128` d'`owuicore-main`, bascule config-only via env vars) |
| Q8 | Visibilité finale du corpus mutualisé (instance / direction / utilisateur) | V2 | Différée explicitement |

---

## 8. Points de vigilance opérationnels

- **yt-dlp est fragile.** YouTube change son API régulièrement. Prévoir une procédure de mise à jour automatisée (cron de bump) ou au minimum une alerte si le provider échoue en masse.
- **Conformité droits d'auteur.** Aucun média n'est redistribué (pas de stockage), seuls les transcripts sont conservés. Mention dans la modale d'import recommandée.
- **CNIL / DR.** Les vidéos publiques YouTube ne posent pas de problème par défaut, mais consigner la décision dans une note doctrine au cas où la question remonterait.
- **Risque de dérive stockage.** Surveiller la taille de la table `Transcript`. Mettre en place une métrique opérationnelle dès V1.
- **Dépendance ASR (fallback).** Si Whisper large-v3 tourne sur GPU partagé avec d'autres charges MirAI, prévoir une file de priorité ou un quota.

---

## 9. Journal d'itération

> Le coding assistant ajoute une entrée à chaque session de travail significative. Format : date, branche/PR, ce qui a été fait, ce qui reste, blocages.

### 2026-05-25 — Cadrage initial
- **Branche** : `feature/youtube-import` (à créer)
- **Fait** : Spec consolidée, principes directeurs actés, modèle de données défini, plan V1/V1.5/V2 posé.
- **Reste** : Tout le développement.
- **Blocages** : Q1 (orchestrateur async) — à résoudre en explorant le repo MirAI avant d'écrire la première ligne de code.

### 2026-05-25 — Création de la branche
- **Branche** : `feature/youtube-import` créée depuis `main` (commit de base = HEAD de `main` au 2026-05-25).
- **Fait** : Branche dédiée créée et spec versionnée dans `features-2-build/FEATURE_video-ingest.md` (le document devient ainsi suivi par Git).
- **Reste** : Tout le développement V1 (cf. section 5). Première étape opérationnelle = résoudre Q1 (orchestrateur async) en explorant le repo avant d'écrire la moindre ligne de code.
- **Blocages** : Q1 (orchestrateur async) — toujours à investiguer.

### 2026-05-25 — Arbitrage Q1 : Postgres natif
- **Fait** : Q1 tranché → orchestrateur Postgres natif (`SELECT … FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY`), zéro nouvelle dépendance (cf. D13). Composant `video-ingest` restera autonome du bus RabbitMQ MirAI.
- **Reste** : démarrer le scaffolding (migration BDD jobs + `VideoSource`/`Transcript`/`UserVideoBookmark`, worker minimal, `YouTubeProvider`).
- **Veille à tenir** : suivre l'évolution des libs Postgres-queue (procrastinate, pgmq, river-py) et basculer si le besoin (volume, fiabilité, ergonomie) le justifie.

### 2026-05-25 — Cadrage mission : service mutualisé extrayable (D14)
- **Fait** : explicitation de la mission — `video-ingest` est un **service mutualisé MirAI** hébergé temporairement dans le repo Mes Réunions (en cours de rename `mirai-mesreunions` → `mirai-mesreunions`), destiné à être extrait dans son propre repo dès maturité V1. Principe 3 réécrit en conséquence (zéro import croisé, schéma BDD isolé, identité opaque, interface REST+MCP only, déployable seul).
- **Impact archi V1** : tables préfixées `video_*` dans un schéma dédié, pas de FK vers les tables MirAI, pas de réutilisation de `libs/shared` côté Mes Réunions, Dockerfile et manifeste K8s autoporteurs sous `services/video-ingest/`.
- **Critère de sortie** : `git filter-repo --path services/video-ingest/` doit produire un repo viable.

### 2026-05-25 — Slice 1 : fondations isolées
- **Fait** :
  - Migration `migrations/internal/019_video_ingest_initial.sql` : 4 tables (`video_sources`, `video_transcripts`, `user_video_bookmarks`, `video_ingest_jobs`), index full-text français généré via `tsvector` STORED, index de dispatch worker `(status, lease_until, created_at)`, zéro FK vers les tables MirAI.
  - Squelette service `services/video_ingest/` (underscore assumé, cf. README — exception vs reste du repo pour rester importable et préfigurer l'extraction).
  - Parseur URL YouTube `services/video_ingest/app/providers/youtube/url.py` : pur, sans réseau, gère watch / youtu.be / shorts / embed / live / m. / music. + variantes scheme et paramètres parasites ; rejette playlists seules, channels, vimeo, dailymotion.
  - Tests unit `tests/unit/test_video_ingest_youtube_url.py` : 37 cas, tous verts.
  - README `services/video_ingest/README.md` matérialise le pacte d'isolation D14 (règles pour chaque future PR).
- **Reste** : `YouTubeProvider.fetch_metadata` + `.fetch_subtitles` (avec yt-dlp + youtube-transcript-api), worker Postgres-native, endpoints REST, outils MCP, intégration Mes Réunions.
- **Blocages** : aucun. Q7 (sortie internet vers youtube.com en environnement souverain) à arbitrer avant le premier appel réseau réel.

### 2026-05-25 — Q7 tranchée (D15) : egress A + B prête
- **Fait** : décision actée (cf. D15). Mode A (egress direct + Cilium FQDN allowlist) en prod-bêta. Mode B (proxy rotatif) **réutilise l'infra existante** d'`owuicore-main/infra/proxy/` — `rotating-proxy.miraiku.svc:3128`, HAProxy + 4 Squid Scaleway, 20 IPs rotatives, Basic Auth — pas de nouvelle infra à déployer. Bascule A→B = config-only (3 env vars + durcissement NetworkPolicy).
- **Implications code (à respecter dès slice 2)** : aucun appel HTTP custom ; tout passe par yt-dlp et youtube-transcript-api qui honorent `HTTP_PROXY`/`HTTPS_PROXY` nativement. Si un futur besoin oblige à coder un appel HTTP, utiliser `requests` avec `trust_env=True` (défaut) — jamais d'URL en dur sans respect des env proxy.
- **Reste** : slice 2 = providers réels (yt-dlp metadata + youtube-transcript-api sous-titres).

### 2026-05-25 — Slice 2 : YouTubeProvider (metadata + sous-titres) + chunking
- **Fait** :
  - `services/video_ingest/app/types.py` — `VideoMetadata`, `TranscriptSegment`, `FetchedTranscript` (dataclasses immuables, pas de SQLAlchemy → testables isolément).
  - `app/providers/base.py` — Protocol `VideoProvider` + hiérarchie d'erreurs (`ProviderError`, `VideoUnavailable`, `SubtitlesUnavailable`).
  - `app/providers/youtube/metadata.py` — wrapper yt-dlp (skip_download=True). Mapping erreurs : private/removed/unavailable/blocked → `VideoUnavailable`, le reste → `ProviderError`. Pas d'appel HTTP custom (D15).
  - `app/providers/youtube/subtitles.py` — wrapper youtube-transcript-api. Préférence manuels > auto. `TranscriptsDisabled` mappé sur `SubtitlesUnavailable`.
  - `app/providers/youtube/__init__.py` — classe `YouTubeProvider` exposant le protocole.
  - `app/chunking.py` — agrégateur 60-90s avec chevauchement 15s (Principe 8). Pur, anti-boucle infinie, gère segments unordered et payloads pathologiques.
  - `services/video_ingest/requirements.txt` — deps service isolées du `requirements.txt` racine (yt-dlp + youtube-transcript-api).
  - Tests : 33 nouveaux cas, **70/70 verts** au total sur la suite `video_ingest_*`. Zéro appel réseau (mocks complets via `unittest.mock`).
- **Bug intéressant rencontré** : algo chunking créait des chunks fantômes par chevauchement quand toute la transcription tenait dans 1 chunk. Fix = sortir explicitement quand `j >= n`.
- **Reste** : slice 3 = worker Postgres-native (claim `SELECT FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY` + lease/heartbeat + watchdog reprise orphelins). Slice 4 = API REST + ingestion pipeline. Slice 5 = MCP. Slice 6 = intégration Mes Réunions. Slice ASR (force_audio + Whisper) plus tard.
- **Blocages** : aucun.

### 2026-05-25 — Slice 3 : worker Postgres-native + orchestrateur
- **Fait** :
  - `app/db.py` — pool psycopg2 propre au service (zéro `libs.shared`), connexion `autocommit` requise pour `LISTEN/NOTIFY`. Config via `VIDEO_INGEST_DATABASE_URL` + `VIDEO_INGEST_DB_MIN/MAX_CONN`.
  - `app/jobs.py` — file de jobs : `enqueue` (+ NOTIFY), `claim_next` (`FOR UPDATE SKIP LOCKED` + bump attempts + lease), `extend_lease`, `complete`, `fail` (avec troncature 2000 chars), `reset_orphans` (watchdog).
  - `app/repo.py` — repositories raw SQL : `find_source_by_provider_id`, `upsert_source` (ON CONFLICT préserve les champs déjà connus), `insert_transcript` (ON CONFLICT remplace), `add_bookmark`, `has_transcript`.
  - `app/orchestrator.py` — pipeline complet : routing provider → lookup dédup → HIT/MISS → fetch metadata → fetch sous-titres (sauf `force_audio`) → chunking → persistance → bookmark. `NeedsAudioFallback` levée quand sous-titres absents (sera interceptée en slice ASR pour requeue). `run_and_record` mappe les exceptions sur les états de la file.
  - `app/worker.py` — boucle principale + 2 threads daemons (heartbeat 30s, watchdog 60s). `LISTEN/NOTIFY` via `select()` sur le socket Postgres avec fallback poll 5s. Identité worker = `hostname:pid` pour le debug.
  - Tests : 22 nouveaux cas (13 orchestrateur + 9 jobs), **92/92 verts** au total. Mocks complets : zéro Postgres, zéro réseau.
- **Bug évité** : la fonction `complete` reset `lease_until` à NULL et `error_message` à NULL — sinon un job retraité après `fail` garderait des résidus.
- **À surveiller** : `claim_next` reprend aussi les `running` au lease dépassé (et pas seulement `pending`), ce qui est volontaire mais signifie qu'un job lent pourrait être pris en parallèle si le heartbeat tombe. Le `attempts +1` permet de détecter ces retries.
- **Reste** : slice 4 = API REST (`POST /video/import` qui enqueue + endpoints lecture) ; slice 5 = MCP ; slice 6 = client Mes Réunions ; slice ASR (force_audio + Whisper) ; slice infra (Dockerfile + manifestes K8s + overlay prod-bêta avec NetworkPolicy mode A).
- **Blocages** : aucun.

### 2026-05-25 — Sessions autonome : slices 4 + 5 + infra livrées, slices 6 + ASR documentées
- **Slice 4 (API REST)** : `app/api.py` (7 endpoints Flask) + `app/auth.py` (vérif JWT autonome via authlib + JWKS, cache 1h, rotation au KID inconnu, décorateurs require_auth/require_admin avec bypass DEV `VIDEO_INGEST_AUTH_DISABLED=1`). 21 nouveaux tests, **113/113 verts**.
- **Slice 5 (MCP)** : `app/mcp_server.py` (FastMCP, transport `streamable-http`, port 8001). 5 tools : `video_import`, `video_get_metadata`, `video_get_transcript`, `video_search`, `video_purge` (avec admin_token via env). Appels directs aux fonctions Python (pas d'aller-retour HTTP loopback). Tests : import-time validé, E2E à faire en intégration.
- **Slice infra** : `services/video_ingest/Dockerfile` autoporteur (pour extraction future, D14). `deploy/kubernetes/environments/prod-beta/internal/video-ingest.yaml` = Deployment API (gunicorn) + Deployment worker + Service ClusterIP + CiliumNetworkPolicy egress mode A (DNS + Postgres + FQDN YouTube + SSO JWKS). Ajouté à la kustomization, `kubectl kustomize` valide.
- **Slice 6 (intégration Mes Réunions)** : non exécutée, documentée en détail dans [INTEGRATION_NOTES.md §1](../services/video_ingest/INTEGRATION_NOTES.md). Contrat API, blueprint Flask suggéré, migration BDD (`meetings.video_source_id` + `video_ingest_job_id`), HTML modale avec **mention légale (Q6 résolue)**, code de polling. À exécuter en main quand l'utilisateur sera disponible (touche du code existant, mieux vaut superviser).
- **Slice ASR** : non exécutée, deux chemins documentés dans [INTEGRATION_NOTES.md §2](../services/video_ingest/INTEGRATION_NOTES.md). Recommandation = chemin A (réutiliser Kevent) plutôt que B (Whisper local). Stub `fetch_audio` ajouté dans `YouTubeProvider` qui lève `NotImplementedError` explicite. Test E2E « zéro audio résiduel » (DoD §10) défini.
- **Checklist d'activation prod-bêta** posée dans INTEGRATION_NOTES.md §3 (migration → secret → kustomize apply → smoke → bout-en-bout).
- **Blocages** : aucun bloqueur technique. Ce qui reste à humaniser : (a) wiring `mesreunions-web` (slice 6), (b) choix chemin ASR + implémentation, (c) Q4 (quotas) et Q5 (audit logging) toujours ouvertes — à câbler avant mise en prod largement ouverte.

### 2026-05-25 — Slices ASR + 6 (backend) livrées en autonomie
- **Slice ASR (chemin A — Kevent)** :
  - `providers/youtube/_kevent.py` — mini-client Kevent autonome (~110 lignes), strict minimum nécessaire (submit + poll), pas de réplication des 602 lignes du client `dmz-to-internal-bridge`. Vars d'env `VIDEO_INGEST_KEVENT_GATEWAY_URL` + `_API_KEY`.
  - `providers/youtube/audio.py` — `fetch_audio_and_transcribe` : yt-dlp `bestaudio[ext=m4a]` dans un `TemporaryDirectory`, appel Kevent, mapping payload Whisper verbose_json → `FetchedTranscript`. **Audio supprimé garanti** à la sortie du `with`.
  - `YouTubeProvider.fetch_audio` câblée à la vraie impl (plus de stub).
  - `orchestrator.run_job` revu : bascule auto sur ASR quand sous-titres absents (`SubtitlesUnavailable` ne lève plus `NeedsAudioFallback`, on essaie d'abord `fetch_audio`). `NeedsAudioFallback` ne reste levée que si `fetch_audio` est elle-même indisponible (`NotImplementedError`).
  - Test E2E **DoD §10 « zéro audio résiduel »** : vérifie que le TemporaryDirectory est bien supprimé après retour, y compris quand Kevent renvoie un payload vide.
  - 7 nouveaux tests (6 audio + 0 orchestrateur réécrits, +1 nouveau).
- **Slice 6 (backend Mes Réunions)** :
  - Patch 2 lignes `modules/auth/routes.py` : `session["access_token"]` stocké au login (sera forwardé en Bearer aux proxys serveur→serveur).
  - Nouveau module `services/mesreunions-web/app/modules/youtube_import/` : 2 routes (`POST /api/youtube/import`, `GET /api/youtube/jobs/<id>`) proxy vers `video-ingest`. Pattern identique à `modules/feedback/`. Env `VIDEO_INGEST_BASE_URL` (défaut = `http://video-ingest.audio-internal.svc.cluster.local:8000`).
  - Blueprint enregistré dans `main.py`.
  - Migration `020_meetings_youtube_link.sql` : `meetings.video_source_id` + `video_ingest_job_id` (pointeurs opaques, **pas de FK cross-service**, D14).
- **Reste vraiment côté humain** :
  - Frontend : ajouter la modale + le polling dans `tabs/meetings.js` (snippet HTML + JS dans INTEGRATION_NOTES.md §1.4).
  - Q4 (quotas anti-abus) et Q5 (audit logging) : toujours ouverts.
- **120/120 tests verts** côté `video_ingest`.

### 2026-05-25 — Q4 + Q5 résolues (quotas + audit)
- **Q5 audit** : migration `021_video_ingest_audit.sql` + `app/audit.py` (log_event, swallow exceptions — l'audit ne tue jamais le flux). Câblé dans `api.import_video` (HIT, MISS, quota dépassé) et `api.purge_source`. Soft pointer sur `video_source_id` (préserve la trace après purge).
- **Q4 quotas** : `app/quotas.py` (`check_import_quota`), fenêtre glissante 24h, HIT cache n'en consomme pas (volontaire : seul le MISS coûte vraiment). Env `VIDEO_INGEST_QUOTA_IMPORTS_PER_DAY` (50 défaut, 0 désactive). HTTP 429 + audit `error` si dépassé.
- 6 nouveaux tests, **126/126 verts**.
- **Reste vraiment** : modale frontend `tabs/meetings.js` (1102 lignes, supervision recommandée) + activation prod-bêta (checklist INTEGRATION_NOTES §3) + revue sécu.

### 2026-05-25 — Frontend modale + revue sécu auto (V1 complète)
- **Frontend** : 3 insertions ADDITIVES dans `tabs/meetings.js` (1102 lignes intactes ailleurs) :
  1. Bouton `🎬 YouTube` dans le header (à côté de `+ Dossier`), `data-action="meetings-new:youtube-import"`.
  2. Case dispatcher → `_openYoutubeImportModal()`.
  3. Bloc fonctions en fin de fichier (`<dialog>` vanilla, mention légale, POST + polling, gestion 429 quota, refresh liste à done).
  Pas de framework ajouté. `node --check` valide la syntaxe.
- **Revue sécu** : [`SECURITY_REVIEW.md`](../services/video_ingest/SECURITY_REVIEW.md) couvre 7 modules + dépendances + surface réseau. 1 fix de défense en profondeur : `auth.py` valide `iss` (env `VIDEO_INGEST_OIDC_ISSUER`). 6 actions restantes documentées (audience+issuer à poser en prod, MCP à protéger si exposé, Dependabot yt-dlp, audit retention, etc.).
- **V1 DoD** : tous les critères backend-checkables ✅. Reste à faire en main = activation prod-bêta (migrations + secrets + apply) + validation tests bout-en-bout en intégration.
- **126/126 tests verts**.

### 2026-05-26 — Wire complet Meeting + section UI « Vidéos web »
- **Contexte** : après le 1er import bout-en-bout (Arthur Mensch / Assemblée nationale, 93 929 chars sous-titres), rien dans la liste « Mes réunions » → trou dans la slice 6 (proxy ne créait pas d'entrée `meetings`).
- **C1 (video-ingest)** : nouvel endpoint `GET /video/my-bookmarks` qui retourne les bookmarks de l'user joints aux metadata source + stats transcript (lang, chars, method). 3 tests unit. **129/129 verts**.
- **C2 (backend mesreunions-web)** :
  - `libs/shared/app/models.py` : ajout `Meeting.video_source_id` + `video_ingest_job_id` (BigInteger, nullable, déjà en BDD via migration 020). Pointeurs opaques cross-service (D14).
  - `device-token-authority` : `create_meeting` accepte les 2 champs, **idempotent** sur `video_ingest_job_id` (renvoie `reused=true` si déjà connu pour ce user_sub). `_meeting_to_dict` les sérialise. `list_meetings?only_video=1` filtre.
  - `mesreunions-web /api/youtube/import` : si HIT cache (reused=true), crée Meeting immédiat. `/api/youtube/jobs/<id>` : si status=done, crée Meeting idempotent. Échec non bloquant.
  - Nouveau `/api/youtube/my-imports` : list meetings YouTube de l'user enrichis par /video/my-bookmarks (titre, durée, transcript stats). Best-effort sur l'enrich.
- **C3 (frontend)** : section additive `#youtube-imports-section` injectée avant `#sessions-list` dans `tabs/meetings.js`. Zéro modif du rendu legacy. Carte par vidéo (titre + channel + durée + badge transcript + lien source). Cache silencieux si 0 items. Refresh au mount + après chaque polling done.
- **C4 deploy** : build + push image + rollout des 4 deployments (video-ingest-api/worker, mesreunions-web, device-token-authority). Smokes OK : health 200, idempotence vérifiée (replay → reused=true même id), endpoint list only_video=1 retourne le meeting.
- **Non-régression** : 129/129 tests unit video_ingest verts. Tous les modules mesreunions-web s'importent (sessions, meetings, youtube_import). Aucun champ enlevé/modifié sur les schémas existants (additif uniquement).
- **À faire au matin** : test bout-en-bout côté UI (hard-refresh service worker pour bypass cache JS) — un nouvel import doit faire apparaître la section « 🎬 Vidéos web importées » avec le titre Mensch.

### _(prochaine entrée à ajouter par le coding assistant)_

---

## 10. Definition of Done (Feature)

Une fois V1 livrée, la feature est considérée *Done* si :

- [ ] Tests unit + intégration passent à 100% sur le composant `video-ingest`
- [ ] Import d'une URL YouTube fr aboutit à un transcript en moins de 30 secondes (sous-titres dispos)
- [ ] Import d'une URL déjà ingérée par un autre utilisateur est `reused=true` en moins de 2 secondes
- [ ] Aucun fichier audio résiduel sur le filesystem après transcription (vérifié par test E2E)
- [ ] 5 outils MCP V1 documentés et appelables depuis un agent MirAI
- [ ] Mes Réunions affiche correctement le statut `youtube_fetching` avec le titre de la vidéo
- [ ] Endpoint `video.purge` testé en environnement de recette
- [ ] README composant à jour, incluant la procédure de bump yt-dlp
- [x] Revue sécurité passée — auto-revue dans [SECURITY_REVIEW.md](../services/video_ingest/SECURITY_REVIEW.md). 1 fix défense en profondeur (validation `iss` JWT optionnelle via env). 6 actions documentées avant ouverture large.
- [ ] Document doctrine succinct produit pour ancrage interministériel (1 page, optionnel mais recommandé)

---

## 11. Liens utiles

- _Branche Git_ : `feature/youtube-import` (à créer depuis `develop`/`main` selon convention repo)
- _Issue / Story SAFe associée_ : _à créer dans l'outil ALM_
- _Spec architecture MirAI (RAG 4 couches)_ : _référence interne_
- _Doctrine de partition / objet métier_ : _référence interne_

---

_Fin du document. Le coding assistant met à jour les sections 1 (date), 6 (décisions nouvelles), 7 (questions résolues), 9 (journal) et 10 (cases cochées) à chaque session significative._
