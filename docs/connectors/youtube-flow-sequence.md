# Flux d'import YouTube — diagramme de séquence + décisions de timing

> Référence : `services/video_ingest/`, `services/dmz-to-internal-bridge/app/external_source.py`,
> `services/mesreunions-web/app/modules/youtube_import/routes.py`.

## 1. Vue d'ensemble

Deux chemins co-existent selon la disponibilité des sous-titres et le
flag `force_audio` :

- **Chemin subtitle** (défaut) : aucun téléchargement audio, transcript
  texte issu de `youtube-transcript-api`, pipeline LLM direct.
- **Chemin force_audio** (depuis Phase B, livrée 2026-05-28) : yt-dlp
  télécharge l'audio en FLAC, transit S3-internal, pipeline standard
  Whisper + pyannote + LLM identique à un upload audio classique.

## 2. Diagramme de séquence — chemin subtitle (HIT cache)

```mermaid
sequenceDiagram
    autonumber
    participant U as User (UI)
    participant MW as mesreunions-web
    participant DTA as device-token-authority
    participant VI as video-ingest API
    participant II as internal-ingester
    participant LLM as LLM chain (thread daemon)

    U->>MW: POST /api/youtube/import {urls[], force_audio=false}
    MW->>DTA: POST /api/v1/meetings (placeholder)
    DTA-->>MW: meeting_id (Meeting créé avec user_audio_file_id=NULL)
    MW->>VI: POST /video/import {url, context=meeting, context_id=meeting_id}

    Note over VI: Cache lookup (provider, provider_video_id)<br/>dans video_sources

    alt HIT cache (transcript déjà ingéré)
        VI->>VI: add_bookmark
        VI->>II: POST /api/v1/external-source/materialize (synchrone)
        Note over II: Idempotent sur (user_sub, external_video_source_id)
        alt UAF n'existe pas pour ce user
            II->>II: Crée UAF virtuel + transcription_text<br/>status=kevent_processing
            II->>II: Link Meeting.user_audio_file_id ← UAF.id
            II->>LLM: spawn thread _run_llm_chain_for_audio
        else UAF existe déjà (re-import après suppression Meeting)
            II->>II: Link new_Meeting.user_audio_file_id ← existing.id<br/>(fix 2026-05-29, commit 4f4e4ca)
        end
        II-->>VI: 200 {reused: bool, audio_file_id}
        VI-->>MW: 200 {status: ready, reused: true}
        MW-->>U: 202 {meeting_id, status: ready}
    else MISS
        VI->>VI: enqueue Postgres job (video_ingest_jobs)
        VI-->>MW: 202 {status: pending, job_id}
        MW-->>U: 202 {meeting_id, job_id}
        Note over VI: Worker reprend en async
        VI->>VI: fetch metadata (yt-dlp metadata-only)
        VI->>VI: fetch_subtitles (youtube-transcript-api)
        VI->>VI: chunk(segments) → 60-90s avec overlap 15s
        VI->>VI: insert video_transcripts (zone interne)
        VI->>II: POST /api/v1/external-source/materialize
        II->>II: Crée UAF + link Meeting + spawn LLM
        II-->>VI: 200
    end

    Note over LLM: Pipeline (5-15 min)
    LLM->>LLM: glossary_correction
    LLM->>LLM: oob_cleaning
    LLM->>LLM: reformulation
    LLM->>LLM: meeting_analysis
    LLM->>LLM: suggest_metadata (filename + meeting_datetime)
    LLM->>LLM: key_points_summary
    LLM->>II: UPDATE user_audio_files (status=kevent_completed)

    U->>MW: poll /api/youtube/my-imports
    MW-->>U: items[].materialization_status='done'<br/>+ suggested_filename + key_points + meeting_analysis
```

## 3. Diagramme de séquence — chemin force_audio (Phase B)

```mermaid
sequenceDiagram
    autonumber
    participant U as User (UI)
    participant MW as mesreunions-web
    participant VI as video-ingest worker
    participant YT as yt-dlp + ffmpeg
    participant II as internal-ingester
    participant K as Kevent gateway
    participant P as pyannote (vm-direct ou Kevent)

    U->>MW: POST /api/youtube/import {urls[], force_audio=true}
    MW->>VI: POST /video/import
    VI->>VI: enqueue job (force_audio=true)
    VI-->>MW: 202 pending

    VI->>YT: yt-dlp bestaudio/best + postprocess FFmpegExtractAudio FLAC
    YT-->>VI: audio.flac (bytes ~50-250 Mo selon durée)

    VI->>II: POST /api/v1/external-source/materialize-audio<br/>multipart (audio_file FLAC + meta JSON)
    II->>II: Upload S3-internal <user_sub>/<synth_code>/<uaf_id>.flac
    II->>II: Crée UAF stored_filename=s3_path<br/>source_type=youtube_audio status=kevent_queued
    II->>II: Link Meeting.user_audio_file_id ← UAF.id
    II->>II: spawn thread _transcribe_via_kevent

    II->>K: POST /jobs/audio (transcription Whisper)
    K-->>II: words[] + segments[] horodatés au mot près
    II->>P: POST /diarize (pyannote — backend selector)
    P-->>II: speaker_segments
    II->>II: merge word↔speaker → speaker_tagged_text
    II->>LLM: chaîne LLM standard (cf. chemin subtitle)
```

## 4. Diagramme — fiche détail UI

```mermaid
sequenceDiagram
    participant U as User
    participant FE as Frontend (meetings.js)
    participant MS as /api/my-sessions
    participant II as internal-ingester

    U->>FE: clic titre row YouTube
    FE->>FE: dispatcher open-detail
    Note over FE: file_id = uaf_id (réel, table user_audio_files)
    FE->>FE: showFileDetail(uaf_id) → activateTab + loadSessions
    FE->>MS: GET /api/my-sessions
    MS->>II: GET /api/v1/audio/external-source-list?user_sub=…
    II-->>MS: items[] UAFs YouTube + LEFT JOIN video_sources<br/>(canonical_url, title, channel, duration_sec)
    MS->>MS: Synth sessions YT injectées + source_type/canonical_url<br/>portés sur synth_upload
    MS-->>FE: sessions[] (audio classique + synth YT)
    FE->>FE: renderFileDetail
    Note over FE: gating data-source-type :<br/>• audio player → iframe YouTube<br/>• bouton "Régénérer transcription+diarisation" masqué
```

## 5. État des timings dans le transcript subtitle (problème ouvert)

### Format actuel

`youtube-transcript-api` retourne des chunks fins (~3-5s) **non
sentence-aligned** :

```json
[
  {"text": "Bonjour", "start": 0.0, "duration": 1.5},
  {"text": "monsieur Mensch je", "start": 1.5, "duration": 1.2},
  {"text": "vous remercie de", "start": 2.7, "duration": 1.0},
  {"text": "votre présence", "start": 3.7, "duration": 1.1}
]
```

`video-ingest` les agrège en chunks 60-90s pour le RAG (cf.
`chunking.chunk()`), donc la granularité descendue à materialize est
trop grossière pour un karaoké au mot près.

### Comparaison avec Whisper

| Niveau | YouTube subtitle | Whisper word-level |
|---|---|---|
| Granularité timing | chunk ~3-5s (puis 60-90s post-agrégation video-ingest) | mot (50-300ms) |
| Sentence boundaries | non | non (juste segments diarisation) |
| Speakers | non | oui (via pyannote merge) |
| Format JSON | `[{start_seconds, end_seconds, text}]` | `[{w, s, e}]` |

### Solutions proposées (cf. § suivant)

Voir `docs/connectors/youtube-karaoke-options.md`.
