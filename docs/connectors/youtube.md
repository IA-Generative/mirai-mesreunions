# Connecteur YouTube (`video-ingest`)

> Premier connecteur de la famille Meeting Source Connectors. Sert de **référence d'implémentation** du contrat (cf. [`meeting-source-connector-spec.md`](../contract/meeting-source-connector-spec.md)).

## Identité

- **provider_id** : `youtube`
- **Code source** : `services/video_ingest/`
- **Dockerfile autoporteur** : `services/video_ingest/Dockerfile`
- **Manifeste K8s** : `deploy/kubernetes/environments/prod-beta/internal/video-ingest.yaml`
- **Migration BDD propre** : `migrations/internal/019_video_ingest_initial.sql` + `021_video_ingest_audit.sql`

## Endpoints exposés

### REST (port 8000)

| Méthode | Chemin | Auth | Description |
|---|---|---|---|
| `POST` | `/video/import` | Bearer JWT | Enqueue un job d'ingestion |
| `GET` | `/video/jobs/<id>` | Bearer | Statut d'un job |
| `GET` | `/video/sources/<id>` | Bearer | Métadonnées d'une source |
| `GET` | `/video/sources/<id>/transcript?format=text\|segments\|markdown` | Bearer | Transcript |
| `GET` | `/video/search?q=…` | Bearer | Recherche full-text (tsvector français) |
| `GET` | `/video/my-bookmarks` | Bearer | Bookmarks de l'user courant |
| `DELETE` | `/video/sources/<id>` | Admin | Purge complète (cascade) |
| `GET` | `/health` | — | Sonde liveness |

### MCP (port 8001, transport streamable-http)

5 tools V1 : `video.import`, `video.get_metadata`, `video.get_transcript`, `video.search`, `video.purge`.

## Configuration (variables d'environnement)

| Variable | Description |
|---|---|
| `VIDEO_INGEST_DATABASE_URL` | DSN PostgreSQL (postgres-internal partagé en prod-bêta) |
| `VIDEO_INGEST_OIDC_JWKS_URL` | URL JWKS Keycloak (sso.mirai.fake-domain.name) |
| `VIDEO_INGEST_OIDC_ISSUER` | Issuer attendu (validation `iss`) |
| `VIDEO_INGEST_OIDC_AUDIENCE` | (optionnel) audience attendue |
| `VIDEO_INGEST_KEVENT_GATEWAY_URL` | URL gateway Kevent (pour fallback ASR Whisper) |
| `VIDEO_INGEST_KEVENT_API_KEY` | API key Kevent (Bearer) |
| `VIDEO_INGEST_MATERIALIZE_URL` | URL endpoint materialize (vide = mode standalone) |
| `VIDEO_INGEST_INTERNAL_API_TOKEN` | Token partagé avec internal-ingester |
| `VIDEO_INGEST_QUOTA_IMPORTS_PER_DAY` | Anti-abus (défaut 50) |
| `VIDEO_INGEST_LEASE_SECONDS` | Lease des jobs worker (défaut 90) |

## Capabilities (à exposer en V4 quand le frontend deviendra dynamique)

```json
{
  "provider_id": "youtube",
  "version": "1.0.0",
  "url_patterns": ["youtube.com/watch", "youtu.be/*", "youtube.com/shorts/*"],
  "supports": {
    "transcript_subtitle": true,
    "transcript_asr": true,
    "audio_download": true,
    "diarization": false,
    "speaker_labels": false,
    "languages": ["fr", "en"]
  },
  "quotas": {"imports_per_day_default": 50},
  "auth": {"oidc_required": true, "admin_role": "video-ingest-admin"}
}
```

## Flux interne (worker)

1. **Claim job** : `SELECT … FOR UPDATE SKIP LOCKED` sur `video_ingest_jobs` (file PostgreSQL native).
2. **Routing** : `YouTubeProvider.matches_url` reconnaît `youtube.com/*`, `youtu.be/*`.
3. **Parsing** : `parse_canonical_id` extrait le `video_id` (11 chars).
4. **Dédup** : lookup `video_sources` par `(provider, provider_video_id)`. HIT → bookmark + done sans re-fetch.
5. **MISS** : `fetch_metadata` (yt-dlp `skip_download=True`) → INSERT `video_sources`.
6. **Sous-titres** : `youtube-transcript-api` (manuels d'abord, auto en fallback) → `video_transcripts.segments_json` (chunks 60-90s, chevauchement 15s).
7. **Fallback ASR** : si `force_audio=True` ou sous-titres absents → `fetch_audio` (yt-dlp `bestaudio`) puis Kevent Whisper.
8. **Hook materialize** : POST vers `internal-ingester` (best-effort, n'invalide pas le job si fail).

## Pièges connus (yt-dlp + Cilium + DSFR)

### yt-dlp casse régulièrement
YouTube change son API à intervalle régulier. Procédure de bump : reconstruire l'image avec `pip install --upgrade yt-dlp` au minimum tous les mois. Alerte à câbler quand le provider échoue en masse (V1.5).

### Cilium FQDN policy
La CNP `video-ingest-egress` autorise `*.youtube.com`, `*.googlevideo.com`, `*.ytimg.com`. **Toute règle FQDN nécessite `rules.dns` sur kube-dns** sinon DNS résout mais TCP renvoie "Network unreachable". Bug rencontré au premier déploiement, fix commit `869adb6`.

### Style modale `<dialog>`
Le DSFR override le style natif `<dialog>`. La modale d'import a son propre `<style>` injecté qui force le centrage (`position:fixed; inset:0; margin:auto`) + backdrop assombri. Sans ça, la modale apparaît top-left non stylée.

### Service Worker PWA cache
Toute modification frontend nécessite un **hard-refresh** côté navigateur pour bypass le service worker. Documenter dans les release notes.

### Conflit `requests` dans la suite de tests
Des tests historiques (test_meeting_intelligence.py, test_glossary_correction_step.py, etc.) font `sys.modules["requests"] = stub` au top-level. Empêche la collection globale `pytest tests/unit/`. Le script `tests/run-regression-campaign.sh` contourne via 2 runs séparés.

## Procédure d'extraction (D14 — quand maturité OK)

Cf. `services/video_ingest/EXTRACTION_CHECKLIST.md` (à compléter en V3).

```bash
git filter-repo \
  --path services/video_ingest/ \
  --path migrations/internal/019_video_ingest_initial.sql \
  --path migrations/internal/021_video_ingest_audit.sql \
  --path tests/unit/test_video_ingest_*.py \
  --path-rename services/video_ingest/:./ \
  --path-rename migrations/internal/:migrations/ \
  --path-rename tests/unit/:tests/
```

Le nouveau repo `mirai-video-ingest` aura :
- `./app/`, `./Dockerfile`, `./requirements.txt`
- `./migrations/019_*.sql`, `./migrations/021_*.sql`
- `./tests/test_video_ingest_*.py`

À ajuster post-extraction : chemins COPY dans Dockerfile, imports relatifs (`services.video_ingest.app.X` → `app.X`).

## Critères d'éligibilité à l'extraction

- [ ] V1 (slices C0-C8) stabilisée 2-4 semaines en prod-bêta
- [ ] Couverture tests video_ingest > 80%
- [ ] Aucun import croisé `services.video_ingest` ← autre service (vérif `grep`)
- [ ] Dockerfile standalone re-buildable hors monorepo (test : `cp -r services/video_ingest /tmp/test && docker build /tmp/test`)
- [ ] Hook materialize confirmé acceptable comme point d'attache permanent OU `_run_llm_chain_for_audio` extrait en lib pure
