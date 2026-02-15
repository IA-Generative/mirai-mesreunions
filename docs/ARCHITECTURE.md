# Architecture & Securite

## Objectif

Le systeme applique un cloisonnement strict entre zone externe et zone interne pour proteger l'identite utilisateur, les tokens de session et les fichiers audio importes.

## Parcours De Lecture

1. `README.md` pour la vue d'ensemble et l'exploitation.
2. `docs/ARCHITECTURE.md` (ce fichier) pour les flux de securite.
3. `tests/DISCOVERY_TEST_PLAN.md` pour la validation utilisateur.
4. `tests/TEST_COVERAGE_STATUS.md` pour le statut de couverture.

## Architecture Logique

```mermaid
flowchart LR
  subgraph EXT["Zone Externe (DMZ)"]
    CG["Code Generator\nOIDC / QR"]
    UP["Upload Portal\nMobile"]
    EOPT["upload_token_options\nauto_transcribe"]
    S3U["S3 upload-staging"]
    AV["Antivirus Worker"]
    TR["Transcode Worker"]
    S3P["S3 processed-staging"]
    FM["File Mover"]
  end

  subgraph INT["Zone Interne"]
    TI["Token Issuer"]
    IOPT["issued_token_options\nauto_transcribe"]
    FP["File Puller"]
    S3I["S3 internal-storage"]
    STT["Transcription Stub\n(conditionnel)"]
    DBI["PostgreSQL interne"]
    MQI["RabbitMQ"]
  end

  CG -->|"issue-token"| TI
  CG --> EOPT
  TI --> IOPT
  UP --> S3U --> AV --> TR --> S3P --> FM
  FM -->|"notify metadata + auto_transcribe"| FP
  FP --> S3I
  FP -->|"si auto_transcribe=true"| STT
```

## Pattern PULL Inter-Zones

```mermaid
sequenceDiagram
  participant FM as File Mover (ext)
  participant FP as File Puller (int)
  participant S3P as S3 processed-staging
  participant S3I as S3 internal-storage

  FM->>FP: POST /api/v1/pull (metadata + auto_transcribe)
  FP->>S3P: GET objet transcode
  FP->>S3I: PUT objet interne
  alt auto_transcribe = true
    FP->>FP: publish queue transcription
  else auto_transcribe = false
    FP->>FP: pas d'enqueue transcription
  end
```

## Flux Generation Token

```mermaid
sequenceDiagram
  participant U as Utilisateur
  participant CG as Code Generator
  participant TI as Token Issuer
  participant PGI as PostgreSQL interne
  participant PGE as PostgreSQL externe

  U->>CG: Login OIDC
  U->>CG: Generer code (TTL, quota, auto_transcribe)
  CG->>TI: POST /api/v1/issue-token
  TI->>PGI: INSERT issued_tokens
  TI->>PGI: INSERT issued_token_options
  TI-->>CG: simple_code + qr_token + token_id
  CG->>PGE: INSERT upload_sessions
  CG->>PGE: INSERT upload_token_options
  CG-->>U: QR code + code court
```

## Pipeline Audio

```mermaid
flowchart TD
  U["Upload mobile"] --> S3U["S3 upload-staging"] --> AV["Scan ClamAV"]
  AV -->|"clean"| TR["Transcode voix\n(optimisation systematique)"]
  AV -->|"infected"| Q["Quarantaine"]
  TR --> S3P["S3 processed-staging"]
  S3P --> FM["file_ready"] --> FP["PULL interne"] --> S3I["S3 internal-storage"]
  FP --> CHK{"auto_transcribe ?"}
  CHK -->|"oui"| STT["Queue transcription -> Stub"]
  CHK -->|"non"| SKIP["Pas de transcription"]
```

## Reseau Et Politiques

```mermaid
flowchart LR
  EXTNS["namespace audio-external"]
  INTNS["namespace audio-internal\n(deny-all ingress)"]
  CG["code-generator"]
  FM["file-mover"]
  TI["token-issuer:8091"]
  FP["file-puller:8090"]

  EXTNS --- CG
  EXTNS --- FM
  INTNS --- TI
  INTNS --- FP

  CG -->|"exception autorisee"| TI
  FM -->|"exception autorisee"| FP
```

## Modeles De Donnees Utiles

- Zone interne:
  - `issued_tokens` (source de verite des tokens)
  - `issued_token_options` (flag `auto_transcribe`)
- Zone externe:
  - `upload_sessions` (suivi d'usage et statut)
  - `upload_token_options` (copie flag `auto_transcribe`)

## Comportement Du Flag auto_transcribe

- Valeur fixee a la creation du token via la checkbox QR.
- Propagee jusqu'a `file-puller` via metadata NOTIFY.
- Effet:
  - `true`: la transcription est mise en file (stub).
  - `false`: pas de mise en file transcription.
- Dans tous les cas, l'audio est optimise pour la voix (antivirus + transcodage).

## Captures Associees

- QR generator: `docs/screenshots/qr-code-gen.png`
- Suivi activite: `docs/screenshots/activity-follow.png`
- Upload mobile: `docs/screenshots/upload-mobile.png`
- Admin: `docs/screenshots/admin-panel.png`
