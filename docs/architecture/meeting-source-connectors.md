# Architecture — Meeting Source Connectors

> Vue d'ensemble du pattern d'ingestion universel de MirAI. Pour la spec normative, voir [`meeting-source-connector-spec.md`](../contract/meeting-source-connector-spec.md). Pour la décision architecturale, voir [`ADR-0003`](../adr/0003-meeting-source-connectors.md).

## En une phrase

> **Tout import de contenu dans MirAI est un connecteur MCP qui matérialise sa source via un endpoint unique, puis le pipeline meeting-intelligence commun produit le compte-rendu.**

## Diagramme — vue système

```
┌─────────────────────────────────────────────────────────────────────────┐
│  CONNECTEURS MCP (1 par source — isolés, extractables, contrat partagé)│
│  ┌──────────────┐  ┌──────────────────┐  ┌──────────────────────────┐  │
│  │ video-ingest │  │  mcp-mcr         │  │  mcp-dictaphone-dinum    │  │
│  │ (YouTube V1) │  │  (V4 planifié)   │  │  (V5 planifié)           │  │
│  └──────────────┘  └──────────────────┘  └──────────────────────────┘  │
│  ┌──────────────┐  ┌──────────────────┐  ┌──────────────────────────┐  │
│  │ mcp-local-   │  │ mcp-pwa-mobile   │  │ mcp-vimeo / -rss / ...   │  │
│  │  files (V7)  │  │ (V8 refacto)     │  │ (V10+)                   │  │
│  └──────────────┘  └──────────────────┘  └──────────────────────────┘  │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                               │ POST /api/v1/external-source/materialize
                               │ Bearer INTERNAL_API_TOKEN
                               │
                               ▼
                ┌─────────────────────────────────────────────┐
                │  INTERNAL-INGESTER (monorepo)               │
                │  ─ Crée UAF virtuel (status=kevent_processing)│
                │  ─ Lance _run_llm_chain_for_audio          │
                │    en thread daemon                         │
                │  ─ Liaison meeting placeholder ←→ UAF ←→    │
                │    video_source_id                          │
                └──────────────┬──────────────────────────────┘
                               │
                               │ pipeline LLM commun (zéro modif)
                               │
                               ▼
        ┌──────────────────────────────────────────────────────────┐
        │ glossary_correction → cleaning → reformulation →         │
        │ meeting_analysis → suggest_metadata → key_points_summary │
        └──────────────────────────────────────────────────────────┘
                               │
                               ▼
        ┌──────────────────────────────────────────────────────────┐
        │ user_audio_files.status = kevent_completed               │
        │ + suggested_filename + meeting_analysis_json +           │
        │   key_points_summary + glossary_corrected_text + ...     │
        └─────────────────────┬────────────────────────────────────┘
                              │
                              ▼
        ┌──────────────────────────────────────────────────────────┐
        │ FRONTEND mesreunions-web                                 │
        │ Liste meetings (audio + YouTube + ... mixés par date)    │
        │ Fiche détail standard (transcript + CR + audio si dispo) │
        └──────────────────────────────────────────────────────────┘
```

## Diagramme — séquence d'un import YouTube

```
User              mesreunions-web         video-ingest          internal-ingester     postgres-internal
 │                       │                      │                       │                      │
 │ POST /api/youtube/    │                      │                       │                      │
 │    import {url}       │                      │                       │                      │
 ├──────────────────────►│                      │                       │                      │
 │                       │ POST /api/v1/meetings (placeholder)          │                      │
 │                       │ (title=URL)          │                       │                      │
 │                       ├─────────────────────────────────────────────►│                      │
 │                       │ {meeting_id=PH}      │                       │ INSERT meetings      │
 │                       │◄─────────────────────────────────────────────┤─────────────────────►│
 │                       │                      │                       │                      │
 │                       │ POST /video/import {url, context_id=PH}      │                      │
 │                       ├─────────────────────►│                       │                      │
 │                       │ {job_id, status=pending}                     │                      │
 │ 202 {meeting_id=PH,   │◄─────────────────────┤                       │                      │
 │  job_id, status=      │                      │                       │                      │
 │  pending}             │                      │                       │                      │
 │◄──────────────────────┤                      │                       │                      │
 │                       │                      │ ── worker async ──    │                      │
 │                       │                      │ fetch_metadata        │                      │
 │                       │                      │ fetch_subtitles       │                      │
 │                       │                      │ INSERT video_sources  │                      │
 │                       │                      │ INSERT video_transcripts                     │
 │                       │                      ├──────────────────────────────────────────────►│
 │                       │                      │                       │                      │
 │                       │                      │ POST /materialize     │                      │
 │                       │                      │ {provider, transcript, segments,             │
 │                       │                      │  meeting_id=PH, vsid}                        │
 │                       │                      ├──────────────────────►│                      │
 │                       │                      │                       │ INSERT user_audio_files
 │                       │                      │                       │ (source_type='youtube_subtitle',
 │                       │                      │                       │  status='kevent_processing',
 │                       │                      │                       │  transcription_text=...)
 │                       │                      │                       ├─────────────────────►│
 │                       │                      │                       │ UPDATE meetings SET  │
 │                       │                      │                       │   user_audio_file_id,│
 │                       │                      │                       │   video_source_id,   │
 │                       │                      │                       │   video_ingest_job_id│
 │                       │                      │                       ├─────────────────────►│
 │                       │                      │                       │ launch thread:       │
 │                       │                      │                       │  _run_llm_chain_for_ │
 │                       │                      │                       │   audio (~30-90s)    │
 │                       │                      │ {audio_file_id, reused=false}                │
 │                       │                      │◄──────────────────────┤                      │
 │                       │                      │ NOTIFY job done       │                      │
 │                       │                      ├──────────────────────────────────────────────►│
 │                       │                      │                       │                      │
 │ GET /api/youtube/     │                      │                       │ ── async, after pipeline ─
 │  jobs/<job_id> (poll) │                      │                       │ UPDATE user_audio_files
 │                       ├─────────────────────►│                       │  SET transcription_status=
 │                       │ GET /video/jobs/...  │                       │   kevent_completed,  │
 │                       │ {status=done,        │                       │   suggested_filename,│
 │                       │  vsid=N}             │                       │   meeting_analysis_json
 │                       │◄─────────────────────┤                       ├─────────────────────►│
 │ 200 {status=done,     │                      │                       │                      │
 │  meeting_id=PH,       │                      │                       │                      │
 │  vsid=N}              │                      │                       │                      │
 │◄──────────────────────┤                      │                       │                      │
 │                       │                      │                       │                      │
 │ GET /api/youtube/     │                      │                       │                      │
 │  my-imports           │                      │                       │                      │
 ├──────────────────────►│                      │                       │                      │
 │                       │ GET /api/v1/meetings?only_video=1&with_audio_preview=1              │
 │                       ├─────────────────────────────────────────────►│ SELECT JOIN UAF      │
 │                       │ {meetings:[{id=PH,   │                       │                      │
 │                       │  suggested_filename, │                       │                      │
 │                       │  key_points,         │                       │                      │
 │                       │  meeting_analysis}]} │                       │                      │
 │                       │◄─────────────────────────────────────────────┤                      │
 │ {items:[{...          │                      │                       │                      │
 │  materialization_     │                      │                       │                      │
 │  status='done',       │                      │                       │                      │
 │  user_audio_file_id}]}│                      │                       │                      │
 │◄──────────────────────┤                      │                       │                      │
```

## Composants du pattern

### Connecteur MCP
- **Isolé** : Python package autonome, idéalement extractable dans son propre repo (cf. D14).
- **Rôle** : authentification source + récupération transcript/segments/audio + mapping vers format canonique.
- **Communique** uniquement via le contrat (REST + MCP + materialize hook).
- **Exemple de référence** : `services/video_ingest/` (YouTube).

### Endpoint materialize
- **Localisation** : `services/dmz-to-internal-bridge/app/puller.py` route `POST /api/v1/external-source/materialize`.
- **Provider-agnostique** : accepte n'importe quel `provider` dans le payload.
- **Idempotent** sur `(user_sub, external_video_source_id)`.
- **Crée** un `user_audio_files` virtuel + lie au Meeting placeholder + lance pipeline LLM en thread daemon.

### Pipeline LLM commun
- **Code éprouvé** : `_run_llm_chain_for_audio()` dans `puller.py` (~750 lignes, sécurisé, retries, watchdog, observabilité).
- **Source-agnostique** : texte en entrée, dict d'updates en sortie. Aucune modification nécessaire pour un nouveau connecteur.
- **Étapes** : glossary_correction → cleaning → reformulation → meeting_analysis → suggest_metadata → key_points_summary.

### Liste meetings unifiée
- **Endpoint** : `GET /api/v1/meetings?only_video=1&with_audio_preview=1` côté `device-token-authority`.
- **Frontend** : `services/mesreunions-web/frontend/tabs/meetings.js` mélange rows audio et rows YouTube triées par date (`renderList`).
- **Status dynamique** : `materialization_status` reflète l'état réel du pipeline (`pending`/`processing`/`done`/`failed`).

## Trajectoire (slices V2 → V10+)

Cf. plan ~/.claude/plans/l-importation-de-fichier-youtube-nifty-frost.md table de livraison.

- **V2** — Player vidéo YouTube + karaoke segment-sync (IFrame API + polling getCurrentTime)
- **V3** — Renommage canonique : `video_source_id` → `source_resource_id` + `source_provider` (alias rétrocompat)
- **V4** — Connecteur `mcp-mcr` (compte-rendu.mirai)
- **V5** — Connecteur `mcp-dictaphone-dinum`
- **V6** — Extension multi-format audio (OGG, FLAC, OPUS, WAV, AAC) via `ALLOWED_AUDIO_EXTENSIONS`
- **V7** — `mcp-local-files` : upload local devient un connecteur formel (capabilities-driven)
- **V8** — Refacto `mcp-pwa-mobile` aligné sur le contrat
- **V9** — Registry MCP central + UI dynamique (menu déroulant « Importer depuis... »)
- **V10+** — Connecteurs supplémentaires (Dailymotion, Vimeo, SRT, podcast RSS, Webex, ...)

## Pièges connus (à NE PAS reproduire)

### Anti-pattern « mcr_import-style »
Le module `services/mesreunions-web/app/modules/mcr_import/` (V0 historique) intègre directement MCR depuis mesreunions-web sans passer par un connecteur isolé. À décommissionner en V4.7.

**Règle** : tout nouveau connecteur DOIT respecter le contrat dès le départ. Pas d'ajout en mode artisanal.

### Pollution sys.modules dans les tests
Plusieurs tests historiques font `sys.modules["requests"] = stub` au top-level → la suite globale `pytest tests/unit/` plante sur les imports `youtube_transcript_api`. Mitigation : campagne `tests/run-regression-campaign.sh` exécute 2 runs séparés.

### Cilium FQDN policy
Toute CNP avec `toFQDNs` doit inclure `rules.dns` sur la règle kube-dns, sinon DNS résout mais TCP renvoie "Network unreachable" silencieux. Référence : `feedback_cilium_fqdn_dns_rules` dans la mémoire `~/.claude`.

## Garanties

- **Aucune régression** sur le chemin audio classique : pipeline LLM inchangé, migration BDD additive + idempotente, modèle SQLAlchemy étendu sans rupture.
- **Évolutivité prouvée** : ajouter un nouveau connecteur ne demande de toucher ni au pipeline LLM, ni à `device-token-authority`, ni au frontend (sauf si on veut un sourceIcon spécifique).
- **D14 respecté** : chaque connecteur reste extractable. video-ingest a déjà son Dockerfile autoporteur + EXTRACTION_CHECKLIST.md.
