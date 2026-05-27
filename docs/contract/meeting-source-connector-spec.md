# Meeting Source Connector — Spec normative (v1.0.0)

> Tout connecteur d'ingestion de réunion externe dans MirAI DOIT respecter ce contrat. Cette spec est référencée par ADR-0003 et matérialise la « norme partagée » qu'évoque le plan d'architecture universelle.

## 1. Identité et configuration

- **`provider_id`** : identifiant stable, kebab-case, ≤ 32 caractères. Exemples : `youtube`, `mcr`, `dictaphone-dinum`, `dailymotion`. Devient la valeur de `meetings.source_provider` et `user_audio_files.source_provider` (V3+).
- **Configuration via variables d'environnement** uniquement, préfixées `MEETING_CONNECTOR_<PROVIDER>_*` (ex. `MEETING_CONNECTOR_YOUTUBE_API_KEY`). Aucun secret dans le code, aucun appel à un service-de-config partagé.
- **Auth utilisateur** : Bearer JWT du realm Keycloak `mirai`. Le connecteur re-vérifie via JWKS autonomement (env `MEETING_CONNECTOR_OIDC_JWKS_URL`).
- **Auth service→service interne** : Bearer `INTERNAL_API_TOKEN` partagé entre le connecteur et `internal-ingester` pour les hooks `materialize` (env `MEETING_CONNECTOR_MATERIALIZE_URL` + `MEETING_CONNECTOR_INTERNAL_API_TOKEN`).

## 2. Surface API minimale

Tout connecteur expose deux interfaces de niveau équivalent : REST + MCP (streamable-http). Les outils MCP délèguent aux mêmes routes REST.

| Endpoint REST | Tool MCP | Sémantique |
|---|---|---|
| `POST /import` | `meeting.import(url, options?)` | Enqueue ou exécute. Réponse : `{job_id, status, reused, source_resource_id, source_canonical_url}`. |
| `GET /jobs/<id>` | `meeting.get_job(id)` | Statut job : `pending`/`running`/`done`/`failed` + détails. |
| `GET /resources/<id>` | `meeting.get_resource(id)` | Métadonnées (titre, durée, channel, published_at, raw metadata). |
| `GET /resources/<id>/transcript?format=text\|segments\|markdown` | `meeting.get_transcript(id, format)` | Transcript dans 3 formats canoniques (cf. §3). |
| `GET /resources/<id>/audio` | `meeting.get_audio(id)` | (optionnel) URL signée vers l'audio extrait. 404 si pas dispo. |
| `GET /search?q=…` | `meeting.search(query)` | (optionnel) Full-text sur les ressources du connecteur. |
| `GET /my-bookmarks` | `meeting.list_my_bookmarks()` | Bookmarks de l'user courant. |
| `DELETE /resources/<id>` | `meeting.purge(id)` | (admin) Purge complète. |
| `GET /capabilities` | `meeting.capabilities()` | Déclare ce que le connecteur sait faire (cf. §5). |
| `GET /health` | — | Sonde liveness. |

Le contrat existant YouTube V1 (`/video/*`) est un renommage progressif : `provider_video_id` → `source_resource_id`, `video_source_id` → `source_resource_id`. Le renommage se fait en V3 du plan avec alias rétrocompatibles.

## 3. Format canonique du transcript

### Transcript texte intégral

`content_text` (str), concaténation des segments avec ` ` (espace).

### Segments

Array de dicts :
```json
[
  {"start_seconds": 0.0, "end_seconds": 4.5, "text": "...", "speaker": "Speaker_01"},
  ...
]
```

- `speaker` : optionnel — présent quand la source fournit une diarisation (Whisper+pyannote, MCR si déjà diarisé, ...).
- Pour les sous-titres YouTube : agrégation par chunks 60-90s avec chevauchement 15s (cf. `services/video_ingest/app/chunking.py`).
- Pour les transcripts MCR : reprendre les blocs `SpeakerTranscription` existants.

### Mots horodatés (synthétique ou réel)

```json
[
  {"w": "Bonjour", "s": 0.1, "e": 0.8},
  ...
]
```

- **Synthétique** si la source ne fournit que des segments (équirépartition sur `[start, end]` du segment — cf. `external_source.flatten_segments_to_synthetic_words`).
- **Réel** si Whisper word-level (cas dictaphone-dinum WhisperX, cas YouTube `force_audio` via Kevent).
- Format strictement identique à celui d'`user_audio_files.transcription_words_json`.

### Markdown horodaté (généré à la demande)

```
**[12s]** Bonjour le monde

**[20s]** Comment allez-vous
```

Pour affichage UI direct. Généré par `format=markdown`.

## 4. Format `materialize` (payload du hook cross-service)

```http
POST /api/v1/external-source/materialize
Authorization: Bearer <INTERNAL_API_TOKEN>
Content-Type: application/json

{
  "provider": "youtube",
  "source_resource_id": "dQw4w9WgXcQ",
  "source_canonical_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "user_sub": "uuid-keycloak-sub",
  "meeting_id": "uuid-placeholder",
  "title": "Titre brut de la vidéo",
  "channel": "Nom du créateur/auteur",
  "duration_sec": 4930,
  "language": "fr",
  "transcript_text": "concat des segments...",
  "segments": [{"start_seconds": 0.0, "end_seconds": 5.0, "text": "..."}, ...],
  "words_json": [{"w": "Bonjour", "s": 0.1, "e": 0.8}, ...],
  "method": "subtitle_manual" | "subtitle_auto" | "asr_whisper" | "external_transcript",
  "external_video_source_id": 1,
  "video_ingest_job_id": 42,
  "extra_metadata": {"...": "tout ce qui ne rentre pas dans le canonique"}
}
```

Réponse normalisée :
```json
{"ok": true, "audio_file_id": "<uuid>", "reused": false, "transcription_status": "kevent_processing"}
```

L'endpoint `materialize` est **provider-agnostique** : il accepte n'importe quel `provider` et fait la même chose (création UAF virtuel + lancement pipeline LLM en thread daemon).

Idempotence garantie sur la paire `(user_sub, external_video_source_id)`.

## 5. Déclaration de capabilities

```json
GET /capabilities
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

Le frontend lit `capabilities` au chargement de la modale d'import et adapte ses inputs (langues dispo, toggle force_audio uniquement si `supports.transcript_asr=true`, etc.).

## 6. Hiérarchie d'erreurs partagée

```
ConnectorError                  — racine
├── ResourceUnavailable         — vidéo privée/retirée/géo-bloquée
├── TranscriptUnavailable       — pas de transcript, pas d'audio téléchargeable
├── ProviderRateLimited         — quota côté plateforme externe
├── AuthError                   — JWT invalide/expiré
├── QuotaExceeded               — quota côté connecteur (user dépasse imports_per_day)
└── ProviderError               — fourre-tout (réseau, parsing, 5xx)
```

Mapping vers codes HTTP : `401`, `403`, `404`, `410`, `429`, `502`.

**Pas de stacktrace exposée** — uniquement messages utilisateur génériques côté API (cf. CodeQL #29 leçon de la V0).

## 7. Observabilité minimale

- **Logs JSON structurés** : `{level, timestamp, provider_id, action, user_sub, resource_id, duration_ms, error_class?}`.
- **Métriques Prometheus** (quand stack à venir) : `meeting_connector_import_total{provider, status}`.
- **Audit DB** : table `*_audit` propre au connecteur ou partagée (cf. `video_ingest_audit` migration 021).

## 8. Discoverabilité côté MCP

Chaque connecteur s'enregistre dans un index MCP central (à définir lors de V9 — Registry MCP). En attendant, le frontend liste les providers via env var `MEETING_CONNECTORS_REGISTRY`.

## 9. Versioning du contrat

**Semver**. Tout connecteur déclare sa version via `/capabilities`. Le monorepo accepte les versions majeures connues (`1.x.x` au lancement). Changement majeur du contrat = nouveau slice plan transverse + ADR de revue.

## 10. Test conformité

Suite partagée `tests/contract/test_meeting_connector_contract.py` (V4) qui, pour un connecteur donné via URL, vérifie :

- Tous les endpoints listés en §2 répondent (200 ou 404 sémantique).
- `/capabilities` retourne un payload valide.
- Le format de transcript respecte §3.
- Les erreurs respectent la hiérarchie §6 via injection de cas mock.

Tout connecteur qui passe cette suite est éligible au pipeline.

## Évolution

- **v1.0.0** (2026-05-27) — version initiale, livrée avec YouTube
- **v1.1.0** (prévu V4) — extension ENUM `source_type` pour `external_transcript`, renommage canonique `source_resource_id`/`source_provider`
- **v2.0.0** (futur) — changements potentiellement breaking si le pattern audio bulk impose de revoir le format `materialize`
