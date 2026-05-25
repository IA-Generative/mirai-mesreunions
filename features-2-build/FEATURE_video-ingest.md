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
| **Dernière mise à jour** | 2026-05-25 — slice 1 livrée : migration 019 + squelette `services/video_ingest/` + parseur URL YouTube (37 tests verts) |

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
- [ ] `YouTubeProvider` (yt-dlp + youtube-transcript-api)
- [x] Normalisation URL YouTube (formats `youtube.com/watch`, `youtu.be`, `shorts`, `embed`, `live`, `&t=`, paramètres parasites ; playlists et channels rejetés)
- [ ] Pipeline d'ingestion async (worker existant à identifier dans le repo — Celery/Temporal/autre)
- [ ] Fallback Whisper large-v3 sous flag `force_audio`
- [ ] **Aucun stockage audio post-transcription** (test E2E à vérifier)
- [ ] Index full-text `tsvector('french', content_text)`
- [ ] Endpoints REST internes + 5 outils MCP V1
- [ ] Intégration Mes Réunions : bouton, modale, statut `youtube_fetching`
- [ ] Endpoint `video.purge` (admin)
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

---

## 7. Questions ouvertes (à arbitrer avant ou pendant le dev)

| # | Question | Échéance souhaitée | Réponse |
|---|---|---|---|
| Q1 | Quel orchestrateur de jobs async utiliser (Celery / Temporal / autre déjà en place dans le repo MirAI) ? | Avant début V1 | **Résolu 2026-05-25 (D13)** : Postgres natif, pas de nouvelle dépendance. À réévaluer si volume ou maturité des libs (procrastinate, pgmq) le justifient |
| Q2 | Iframe YouTube standard ou composant maison pour lecteur horodaté ? | V1.5 | _Ouvert_ |
| Q3 | Quel modèle pour post-traitement LLM (Mistral Small via API MirAI ?) ? | V1.5 | _Ouvert_ |
| Q4 | Quotas par utilisateur sur les imports (anti-abus) ? | V1 ou V1.5 | _Ouvert_ |
| Q5 | Logging d'audit : qui a importé quoi quand (pour traçabilité interne) ? | V1 | _Ouvert_ |
| Q6 | Mention légale dans la modale d'import (responsabilité droits) ? | V1 | _Ouvert — recommandé_ |
| Q7 | Sortie internet vers youtube.com : passerelle/proxy à configurer en environnement souverain ? | V1 | _À documenter dans README_ |
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
- [ ] Revue sécurité passée (au minimum : check des dépendances, exposition MCP, ACL endpoints admin)
- [ ] Document doctrine succinct produit pour ancrage interministériel (1 page, optionnel mais recommandé)

---

## 11. Liens utiles

- _Branche Git_ : `feature/youtube-import` (à créer depuis `develop`/`main` selon convention repo)
- _Issue / Story SAFe associée_ : _à créer dans l'outil ALM_
- _Spec architecture MirAI (RAG 4 couches)_ : _référence interne_
- _Doctrine de partition / objet métier_ : _référence interne_

---

_Fin du document. Le coding assistant met à jour les sections 1 (date), 6 (décisions nouvelles), 7 (questions résolues), 9 (journal) et 10 (cases cochées) à chaque session significative._
