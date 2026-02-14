# Secure Audio Upload Pipeline

> Système sécurisé d'upload audio par QR code avec cloisonnement zone externe / zone interne, génération de tokens côté interne, analyse antivirale, transcodage et transcription automatique.

## Principe fondamental

**La zone interne est l'autorité de confiance.** Aucun identifiant de session n'est généré côté externe. Le `token-issuer` (zone interne) est la seule source de vérité pour les codes d'upload. La zone externe ne fait que relayer et consommer ces tokens — elle ne peut en aucun cas en forger.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          ZONE EXTERNE (DMZ)                              │
│                                                                          │
│   ┌─────────────────┐         ┌─────────────────┐                        │
│   │  Code Generator  │────────▶│  Upload Portal   │                       │
│   │  (OIDC/Keycloak) │  QR url │  (page mobile)   │                       │
│   └────────┬────────┘         └────────┬─────────┘                       │
│            │                           │                                  │
│   demande  │                   upload  │                                  │
│   token    │                   fichier │                                  │
│            │                           ▼                                  │
│            │                  ┌─────────────────┐                         │
│            │                  │ S3: upload-staging│                        │
│            │                  └────────┬─────────┘                        │
│            │                           │                                  │
│            │                  ┌────────▼─────────┐  ┌──────────────────┐  │
│            │                  │   AV Worker       │  │ Transcode Worker │  │
│            │                  │   (ClamAV)        │─▶│ (FFmpeg)         │  │
│            │                  └──────────────────┘  └────────┬─────────┘  │
│            │                                                  │           │
│            │                                         ┌────────▼────────┐  │
│            │                                         │S3: processed-   │  │
│            │                                         │    staging      │  │
│            │                                         └────────┬────────┘  │
│            │                                                  │           │
│            │                                         ┌────────▼────────┐  │
│            │                                         │  File Mover     │  │
│            │                                         │  (notificateur) │  │
│            │                                         └────────┬────────┘  │
└────────────┼──────────────────────────────────────────────────┼───────────┘
             │                                                  │
        API token                                        NOTIFY (metadata)
      (bearer auth)                                     (bearer auth)
             │                                                  │
┌────────────▼──────────────────────────────────────────────────▼───────────┐
│                           ZONE INTERNE                                    │
│                                                                           │
│   ┌─────────────────┐                                                     │
│   │  Token Issuer    │◀── Seule autorité de génération                    │
│   │  (simple_code +  │    des tokens de session                           │
│   │   qr_token)      │                                                    │
│   └─────────────────┘                                                     │
│                                                                           │
│   ┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐  │
│   │  File Puller     │───▶│ S3: internal-    │───▶│ Transcription Stub  │  │
│   │  (PULL depuis    │    │     storage      │    │ (simule API STT)    │  │
│   │   processed S3)  │    └──────────────────┘    └─────────────────────┘  │
│   └─────────────────┘                                                     │
│                                                                           │
│   ┌─────────────────┐    ┌──────────────────┐                             │
│   │ PostgreSQL       │    │ RabbitMQ         │                             │
│   │ (interne)        │    │ (interne)        │                             │
│   └─────────────────┘    └──────────────────┘                             │
└───────────────────────────────────────────────────────────────────────────┘
```

## Flux de génération de token (interne → externe)

```
 Utilisateur          Code Generator (ext)         Token Issuer (int)         PostgreSQL (int)
     │                        │                            │                        │
     │── login OIDC ─────────▶│                            │                        │
     │                        │                            │                        │
     │── "Générer un code" ──▶│                            │                        │
     │                        │── POST /issue-token ──────▶│                        │
     │                        │   {user_sub, ttl, max}     │── generate code ──────▶│
     │                        │                            │── generate qr_token ──▶│
     │                        │                            │── INSERT issued_tokens ▶│
     │                        │◀─ {simple_code, qr_token} ─│                        │
     │                        │                            │                        │
     │                        │── INSERT upload_sessions   │                        │
     │                        │   (copie locale, suivi)    │                        │
     │                        │                            │                        │
     │◀── QR code + code ────│                            │                        │
```

Le code-generator **ne contient aucune logique de génération de token**. Il délègue à 100% au token-issuer via API authentifiée (bearer token). La table `issued_tokens` en zone interne fait foi.

## Composants

| Service | Zone | Port | Rôle |
|---------|------|------|------|
| **code-generator** | Externe | 8080 | Interface OIDC, demande de token au token-issuer interne, affiche QR |
| **upload-portal** | Externe | 8081 | Page mobile d'upload audio (QR/code), WebSocket temps réel |
| **antivirus-worker** | Externe | — | Scan ClamAV, quarantaine si virus |
| **transcode-worker** | Externe | — | FFmpeg : loudnorm dual-pass (linear), highpass 80Hz, lowpass 7kHz, limiter, score qualité 1-5 |
| **file-mover** | Externe | — | Notifie la zone interne qu'un fichier est prêt (metadata uniquement) |
| **token-issuer** | **Interne** | 8091 | **Autorité unique** de génération des tokens (simple_code + qr_token) |
| **file-puller** | Interne | 8090 | Tire les fichiers transcodés depuis S3 processed-staging |
| **transcription-stub** | Interne | — | Simule la transcription STT (remplaçable par Whisper/Azure) |

## Principes de sécurité

1. **Tokens générés côté interne** — Le `token-issuer` est la seule autorité. La zone externe ne peut pas forger de codes de session. En cas de compromission DMZ, aucun token frauduleux ne peut être créé.

2. **Pattern PULL strict** — Les données ne sont jamais poussées vers l'intérieur. La zone externe *notifie* (metadata JSON), la zone interne *tire* le fichier depuis S3.

3. **Deux points d'entrée contrôlés** — La zone interne n'expose que deux services via NetworkPolicy :
   - `token-issuer:8091` ← accessible uniquement par `code-generator`
   - `file-puller:8090` ← accessible uniquement par `file-mover`

4. **3 stockages S3 séparés** — `upload-staging` (bruts), `processed-staging` (transcodés, zone bridge), `internal-storage` (comptes usagers, zone interne uniquement)

5. **Codes éphémères** — QR codes avec TTL configurable (15 min → 3 jours), limite de 5 uploads par session (configurable)

6. **Analyse antivirale obligatoire** — Tout fichier passe par ClamAV. Fichiers infectés en quarantaine.

## Démarrage rapide

### Docker Compose

```bash
# Cloner le repo
git clone https://github.com/votre-org/secure-audio-upload.git
cd secure-audio-upload

# Copier la config
cp configs/.env.example configs/.env

# Lancer (script automatisé)
bash deploy/scripts/setup.sh
```

Ou manuellement :

```bash
docker compose -f deploy/docker/docker-compose.yml up -d
```

### Compatibilité AMD64 / ARM64

La stack Docker Compose est compatible `linux/amd64` et `linux/arm64` :
- Images infra multi-arch (PostgreSQL, RabbitMQ, MinIO, Keycloak, ClamAV)
- Image applicative basée sur `python:3.12-slim` (multi-arch)
- Les images infra du `docker-compose.yml` sont figées par digest (`image: tag@sha256:...`) pour une exécution reproductible sur les deux architectures.

Pour forcer un test sur une architecture donnée :

```bash
# Test amd64
DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose -f deploy/docker/docker-compose.yml up -d --build

# Test arm64
DOCKER_DEFAULT_PLATFORM=linux/arm64 docker compose -f deploy/docker/docker-compose.yml up -d --build
```

Mise à jour des digests (quand nécessaire) :

```bash
docker buildx imagetools inspect <image:tag> | sed -n '1,6p'
```

## Mode d'emploi

### 1. Démarrer la stack

```bash
docker compose -f deploy/docker/docker-compose.yml up -d --build
```

Pour forcer les URLs générées (QR/code) sur l'IP publique ou LAN du serveur :

```bash
PUBLIC_HOST=<IP_PUBLIQUE_OU_LAN> docker compose -f deploy/docker/docker-compose.yml up -d --build
```

Exemple : `PUBLIC_HOST=192.168.1.50`
Important : ouvre aussi le Code Generator via cette même IP (`http://<IP>:8080`) et pas via `localhost`.
Note : `PUBLIC_HOST` est prioritaire pour la génération des URLs QR (`http://<PUBLIC_HOST>:8081/upload/...`).

### 2. Vérifier que tout est démarré

```bash
docker compose -f deploy/docker/docker-compose.yml ps
```

Vérifications rapides :

```bash
curl -sS http://localhost:8090/health
curl -sS http://localhost:8091/health
```

### 3. Utiliser l'application (web)

1. Ouvrir le code generator : `http://localhost:8080`
2. Se connecter via OIDC (Keycloak)
3. Générer un code/QR
   - En mode test Docker Compose, des durées courtes `15s` et `30s` sont disponibles
4. Ouvrir le portail d'upload : `http://localhost:8081`
5. Uploader un fichier audio et suivre les statuts
   - Une fenêtre de grâce après expiration (`UPLOAD_EXPIRY_GRACE_SECONDS`) permet de finir un upload en cours.
   - Purge automatique côté upload: exécution quotidienne, suppression des fichiers de plus de 12h.

### 4. Utiliser l'application (mobile, même Wi-Fi)

1. Trouver l'IP locale de la machine hôte (ex: `192.168.x.x`)
2. Accéder depuis le mobile :
   - `http://<IP_LOCALE>:8080`
   - `http://<IP_LOCALE>:8081`
   - `http://<IP_LOCALE>:8082` (admin)
3. Les QR codes générés utiliseront cette IP (et non `localhost`) si `PUBLIC_HOST` est défini.

### 5. Suivi administration

- Admin Portal : `http://localhost:8082`
- Fonctions disponibles :
  - suivi sessions/fichiers pipeline
  - suivi transcription (statuts + journal des appels stub STT)
  - affichage impact de normalisation (LUFS/TP/LRA avant/après + delta) directement dans la liste des fichiers
  - visualisation S3 (`upload`, `processed`, `internal`)
  - téléchargement d'objets S3

### 5.bis Interface code generator (QR)

- Dans la liste des fichiers:
  - le nom long est forcé à la ligne pour rester lisible dans le bloc gris clair
  - `Télécharger` et `Écouter` sont disponibles pour chaque fichier
- `2.5/5 (valeur maximale)` = indice de qualité audio (score 1 à 5)
  - un infobulle `i` décrit le calcul (RMS, ratio de silence, durée, fréquence d'échantillonnage)
- Bouton `Purger liste + fichiers`:
  - supprime la liste de sessions côté utilisateur
  - supprime les objets audio associés dans les buckets externes
- Bouton `Impact normalisation` (par fichier transcodé):
  - affiche une comparaison avant/après (`LUFS`, `True Peak`, `LRA`) et les deltas

### 6. Sécurité API interne

- `API-token` (`/api/v1/issue-token`, `/api/v1/validate-token`) : authentification obligatoire par header
  `Authorization: Bearer <INTERNAL_API_TOKEN>`.
- `NOTIFY` (`/api/notify-status`) : authentification obligatoire par le même header Bearer.
- Vérification de token en comparaison constante (`hmac.compare_digest`).
- Les services refusent de démarrer si `INTERNAL_API_TOKEN` est faible (minimum 32 caractères, pas de placeholder
  type `change-me`, `dev-`, `test-`, etc.).

### Accès local (sans exposer d'information sensible)

| Service | URL | Authentification |
|---------|-----|------------------|
| Code Generator | http://localhost:8080 | OIDC Keycloak (utilisateurs via variables/realm) |
| Upload Portal | http://localhost:8081 | accès par code/QR |
| Admin Portal | http://localhost:8082 | OIDC Keycloak + filtre admin |
| Token Issuer (API) | http://localhost:8091/health | API interne (bearer token) |
| Keycloak Admin | http://localhost:8180 | compte admin défini par configuration |
| RabbitMQ | http://localhost:15672 | identifiants via variables d'environnement |
| MinIO Upload | http://localhost:9001 | identifiants via variables d'environnement |
| MinIO Processed | http://localhost:9003 | identifiants via variables d'environnement |
| MinIO Internal | http://localhost:9005 | identifiants via variables d'environnement |

### Docker Compose (identifiants de test uniquement)

Les identifiants ci-dessous sont **uniquement pour un environnement local de test**.  
Ils ne doivent jamais être réutilisés en intégration/production.

| Service | URL | Identifiants de test |
|---------|-----|----------------------|
| Code Generator (OIDC user) | http://localhost:8080 | `testuser` / `testpassword` |
| Admin Portal (OIDC user) | http://localhost:8082 | `admin` / `adminpassword` (test, change-me en prod) |
| Keycloak Admin | http://localhost:8180 | `admin` / `admin` (test, change-me en prod) |
| RabbitMQ | http://localhost:15672 | `audio` / `change-me-rabbit` |
| MinIO Upload | http://localhost:9001 | `minioadmin` / `minioadmin` (test, change-me en prod) |
| MinIO Processed | http://localhost:9003 | `minioadmin` / `minioadmin` (test, change-me en prod) |
| MinIO Internal | http://localhost:9005 | `minioadmin` / `minioadmin` (test, change-me en prod) |

Pour générer un token interne robuste :

```bash
python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
```

### Kubernetes

```bash
# Namespaces + NetworkPolicies
kubectl apply -f deploy/kubernetes/shared/namespaces.yaml

# Secrets (éditer les valeurs avant !)
kubectl apply -f deploy/kubernetes/shared/secrets.yaml

# Zone externe (namespace: audio-external)
kubectl apply -f deploy/kubernetes/external-zone/

# Zone interne (namespace: audio-internal)
kubectl apply -f deploy/kubernetes/internal-zone/
```

## Isolation réseau

### Docker Compose (3 réseaux)

| Réseau | Services | Rôle |
|--------|----------|------|
| `external-net` | code-generator, upload-portal, admin-portal, workers, ClamAV, MinIO upload/processed, PostgreSQL ext | Zone DMZ |
| `internal-net` | token-issuer, file-puller, transcription-stub, admin-portal, MinIO internal, PostgreSQL int | Zone interne |
| `dmz-net` | code-generator ↔ token-issuer, file-mover ↔ file-puller | Bridge contrôlé (2 flux seulement) |

### Kubernetes (NetworkPolicies)

```yaml
# Zone interne : deny-all par défaut
# Exception 1 : token-issuer:8091 ← code-generator (audio-external)
# Exception 2 : file-puller:8090  ← file-mover (audio-external)
# Trafic intra-zone interne : autorisé
```

## Configuration

Variables d'environnement principales (`configs/.env.example`) :

| Variable | Défaut | Description |
|----------|--------|-------------|
| `CODE_TTL_MINUTES` | `15` | Durée de validité des codes |
| `CODE_TTL_MAX_MINUTES` | `4320` | TTL max (3 jours) |
| `ALLOW_SHORT_QR_TTL_SECONDS_TEST` | `false` | Autorise les TTL de test `15s`/`30s` |
| `MAX_UPLOADS_PER_SESSION` | `5` | Uploads max par code |
| `CODE_LENGTH` | `6` | Longueur du code simple |
| `UPLOAD_STATUS_VIEW_TTL_MINUTES` | `60` | Durée de consultation du statut après expiration |
| `UPLOAD_EXPIRY_GRACE_SECONDS` | `300` | Fenêtre de grâce pour terminer un upload après expiration du code |
| `EXTERNAL_PURGE_INTERVAL_SECONDS` | `86400` | Fréquence de purge automatique côté upload portal |
| `EXTERNAL_PURGE_MAX_AGE_HOURS` | `12` | Âge max des fichiers externes avant purge |
| `INTERNAL_PURGE_INTERVAL_SECONDS` | `86400` | Fréquence de purge automatique côté file-puller |
| `INTERNAL_PURGE_MAX_AGE_DAYS` | `7` | Âge max des fichiers importés côté intranet avant purge |
| `NORMALIZATION_CACHE_TTL_SECONDS` | `3600` | Durée du cache des métriques de normalisation côté admin |
| `NORMALIZATION_MAX_COMPUTE_PER_REFRESH` | `0` | Nombre max d'analyses de normalisation lancées par refresh dashboard (0 = non bloquant) |
| `NORMALIZATION_ANALYSIS_MAX_SECONDS` | `180` | Durée max de l'échantillon analysé pour l'impact de normalisation (page QR/interne) |
| `TOKEN_ISSUER_API_URL` | `http://token-issuer:8091/api/v1/issue-token` | URL du token-issuer interne |
| `INTERNAL_API_TOKEN` | — | Bearer token partagé inter-zones |
| `PUBLIC_HOST` | — | Hôte/IP publique utilisée pour les URLs générées (QR + redirects) |
| `OIDC_ISSUER` | — | URL Keycloak |
| `OIDC_INTERNAL_ISSUER` | `http://keycloak:8080/realms/audio-upload` | URL Keycloak utilisée par les services Docker pour les appels serveur-à-serveur OIDC |
| `FFMPEG_AUDIO_FILTER` | `highpass=f=80,lowpass=f=7000,loudnorm=...` | Filtre FFmpeg voix |
| `ENABLE_LOUDNORM` | `true` | Active/desactive `loudnorm` dans le worker de transcodage (mode dual-pass `linear=true`) |
| `POST_LOUDNORM_FILTER_CHAIN` | `highpass=f=80,lowpass=f=7000,alimiter=limit=0.95` | Filtres appliqués après loudnorm (ordre strict) |

## Mesure de l'impact de normalisation

Script local:

```bash
python deploy/scripts/measure_normalization_impact.py \
  --source /chemin/source.wav \
  --normalized /chemin/normalise.wav
```

JSON:

```bash
python deploy/scripts/measure_normalization_impact.py \
  --source /chemin/source.wav \
  --normalized /chemin/normalise.wav \
  --json
```

## Pipeline de traitement audio

```
Upload mobile → S3 upload-staging → ClamAV scan
                                        │
                              ┌─────────┴──────────┐
                              │                     │
                           CLEAN                 INFECTED
                              │                     │
                        FFmpeg transcode        Quarantaine
                        (loudnorm EBU R128 dual-pass,
                         highpass 80Hz, lowpass 7kHz,
                         limiter anti-pics,
                         16kHz mono WAV)
                              │
                        Score qualité 1-5
                              │
                     S3 processed-staging
                              │
                    NOTIFY → PULL → S3 internal-storage
                                        │
                                  Transcription STT
```

## Formats audio supportés

MP3, WAV, OGG, FLAC, M4A, AAC, WMA, OPUS, WEBM

## Arborescence du projet

```
secure-audio-upload/
├── configs/.env.example
├── deploy/
│   ├── docker/
│   │   ├── docker-compose.yml          # 3 réseaux isolés
│   │   └── keycloak-realm.json         # Realm Keycloak dev
│   └── kubernetes/
│       ├── shared/
│       │   ├── namespaces.yaml         # Namespaces + NetworkPolicies
│       │   └── secrets.yaml
│       ├── external-zone/
│       │   └── deployments.yaml        # code-gen, upload-portal, workers, ClamAV, Ingress
│       └── internal-zone/
│           └── deployments.yaml        # token-issuer, file-puller, transcription, PostgreSQL
├── docs/ARCHITECTURE.md
├── libs/shared/app/
│   ├── config.py                       # Config centralisée
│   ├── models.py                       # SQLAlchemy (IssuedToken, UploadSession, UploadedFile, UserAudioFile)
│   ├── database.py                     # Session factories
│   ├── s3_helper.py                    # Opérations S3/MinIO
│   └── queue_helper.py                 # RabbitMQ publish/consume
├── services/
│   ├── code-generator/app/main.py      # OIDC + appel token-issuer + QR
│   ├── upload-portal/app/
│   │   ├── main.py                     # Upload API + WebSocket
│   │   └── templates/                  # Pages HTML mobile
│   ├── antivirus-worker/app/main.py    # ClamAV consumer
│   ├── transcode-worker/app/main.py    # FFmpeg consumer
│   ├── file-mover/app/
│   │   ├── main.py                     # Notificateur (zone ext)
│   │   └── puller.py                   # File Puller API (zone int)
│   ├── token-issuer/app/main.py        # Génération tokens (zone int)
│   └── transcription-stub/app/main.py  # Stub STT (zone int)
├── deploy/scripts/setup.sh
├── deploy/docker/Dockerfile
├── requirements.txt
└── README.md
```

## Licence

MIT
