# Secure Audio Upload Pipeline

> Système sécurisé d'upload audio par QR code avec cloisonnement zone externe / zone interne, génération de tokens côté interne, analyse antivirale, transcodage et transcription automatique.

## Principe fondamental

**La zone interne est l'autorité de confiance.** Aucun identifiant de session n'est généré côté externe. Le `token-issuer` (zone interne) est la seule source de vérité pour les codes d'upload. La zone externe ne fait que relayer et consommer ces tokens — elle ne peut en aucun cas en forger.

## Architecture

```mermaid
flowchart LR
  subgraph EXT["ZONE EXTERNE (DMZ)"]
    CG["Code Generator<br/>(OIDC/Keycloak)"]
    UP["Upload Portal<br/>(mobile)"]
    S3U["S3 upload-staging"]
    AV["AV Worker<br/>(ClamAV)"]
    TR["Transcode Worker<br/>(FFmpeg)"]
    S3P["S3 processed-staging"]
    FM["File Mover<br/>(notificateur)"]

    CG -->|"QR url"| UP
    UP -->|"upload fichier"| S3U
    S3U --> AV --> TR --> S3P --> FM
  end

  subgraph INT["ZONE INTERNE"]
    TI["Token Issuer<br/>(autorité unique token)"]
    FP["File Puller<br/>(PULL depuis processed S3)"]
    S3I["S3 internal-storage"]
    STT["Transcription Stub"]
    DB["PostgreSQL"]
    MQ["RabbitMQ"]

    FP --> S3I --> STT
  end

  CG -->|"API token<br/>(Bearer auth)"| TI
  FM -->|"NOTIFY metadata<br/>(Bearer auth)"| FP
```

## Flux de génération de token (interne → externe)

```mermaid
sequenceDiagram
  participant U as Utilisateur
  participant CG as Code Generator (ext)
  participant TI as Token Issuer (int)
  participant PG as PostgreSQL (int)

  U->>CG: login OIDC
  U->>CG: Générer un code
  CG->>TI: POST /issue-token {user_sub, ttl, max}
  TI->>PG: generate code + qr_token
  TI->>PG: INSERT issued_tokens
  TI-->>CG: {simple_code, qr_token}
  CG->>PG: INSERT upload_sessions (copie locale/suivi)
  CG-->>U: QR code + code
```

Le code-generator **ne contient aucune logique de génération de token**. Il délègue à 100% au token-issuer via API authentifiée (bearer token). La table `issued_tokens` en zone interne fait foi.

## Composants

| Service | Zone | Port | Rôle |
|---------|------|------|------|
| **code-generator** | Externe | 8080 | Interface OIDC, demande de token au token-issuer interne, affiche QR |
| **upload-portal** | Externe | 8081 | Page mobile d'upload audio (QR/code), WebSocket temps réel |
| **antivirus-worker** | Externe | — | Scan ClamAV, quarantaine si virus |
| **transcode-worker** | Externe | — | FFmpeg : loudnorm dual-pass (linear), highpass 80Hz, lowpass 7kHz, limiter, score qualité 1-5 |
| **file-mover** | Externe | — | Notifie la zone interne qu'un fichier est prêt (metadata uniquement), avec retries RabbitMQ |
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

7. **Transfert idempotent** — Si une notification `file_ready` est rejouée (retry réseau/queue), le `file-puller` détecte le fichier déjà importé et répond `already_pulled` sans doublonner les données.

8. **Enrôlement persistant device navigateur** — Le portail upload enrôle le navigateur (token device persistant), vérifie sa validité à chaque initialisation et permet la révocation unitaire/globale côté QR interne et admin.

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

Ou avec détection automatique de l'IP hôte (recommandé pour tests mobile/LAN):

```bash
./deploy/scripts/compose-up.sh
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
- Gestion des appareils enrôlés:
  - liste des devices du compte utilisateur
  - renommage d'un device
  - révocation d'un device
  - révocation globale des devices du compte

### 5.ter Enrôlement device (upload)

- À l'ouverture du lien QR, le navigateur:
  - tente de réutiliser un `device_token` persistant (`localStorage`)
  - sinon déclenche un enrôlement initial (clé device + fingerprint)
- Chaque requête upload/status envoie le header `X-Device-Token`.
- Le backend applique:
  - fast-path local (signature + rétention),
  - validation backend forte à l'initialisation de session (détection rapide des révocations),
  - puis validation asynchrone backend périodique.
- Si la validation backend échoue au-delà de la fenêtre configurée, les requêtes sont refusées avec message explicite de rescanner/régénérer un code.

### 6. Sécurité API interne

- `API-token` (`/api/v1/issue-token`, `/api/v1/validate-token`) : authentification obligatoire par header
  `Authorization: Bearer <INTERNAL_API_TOKEN>`.
- `NOTIFY` (`/api/notify-status`) : authentification obligatoire par le même header Bearer.
- Vérification de token en comparaison constante (`hmac.compare_digest`).
- Les services refusent de démarrer si `INTERNAL_API_TOKEN` est faible (minimum 32 caractères, pas de placeholder
  type `change-me`, `dev-`, `test-`, etc.).

### 7. Créer des comptes de test Keycloak (script local non versionné)

Un wrapper local est fourni pour éviter d'exposer des credentials admin dans Git.

1. Copier le fichier d'exemple :

```bash
cp deploy/kubernetes/scripts/create-keycloak-test-users.local.env.example \
   deploy/kubernetes/scripts/create-keycloak-test-users.local.env
```

2. Modifier localement `deploy/kubernetes/scripts/create-keycloak-test-users.local.env`
   avec les vraies valeurs `KEYCLOAK_ADMIN_USER` et `KEYCLOAK_ADMIN_PASSWORD`.

3. Lancer la création des comptes :

```bash
./deploy/kubernetes/scripts/create-keycloak-test-users.local.sh
```

Le script crée/met à jour par défaut `testuser01` à `testuser10`.

### 8. Scénario de test bout-en-bout (E2E)

1. Générer une session/QR via `https://import-audio.fake-domain.name`.
2. Depuis mobile, ouvrir le lien QR et uploader un audio court.
3. Vérifier la progression du statut : `uploaded` -> `scanned` -> `transcoded` -> `transferred`.
4. Contrôler côté admin (`http://localhost:8082`) que la session apparaît avec ses événements.
5. Vérifier la présence des objets dans les buckets :
   - `audio-upload` / `ingate-audio` pour l'entrée,
   - `audio-processed` pour le transcodé,
   - `audio-internal` après transfert interne.
6. Tester lecture et téléchargement des fichiers source/transcodé depuis l'interface.
7. Vérifier la transcription stub et le journal des appels dans l'admin.

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

Autoscaling Kubernetes configuré:
- `transcode-worker` via KEDA sur la queue `transcode` (jusqu'à 50 replicas)
- `file-mover` via KEDA sur la queue `file_ready` (jusqu'à 50 replicas)
- `transcription-stub` via KEDA sur la queue `transcription` (jusqu'à 20 replicas)
- `file-puller` via HPA CPU/Mémoire (1 à 20 replicas)

### Runbook debug transfert (Kubernetes)

Quand un fichier reste bloqué en `transferring` ou `transcoded`, vérifier dans cet ordre:

```bash
# 1) Santé pods
kubectl -n audio-external get pods
kubectl -n audio-internal get pods

# 2) Autoscaling actif
kubectl -n audio-external get scaledobject
kubectl -n audio-internal get scaledobject
kubectl -n audio-internal get hpa

# 3) Backlog RabbitMQ (queue file_ready/transcode/transcription)
kubectl -n audio-external logs deploy/rabbitmq --tail=200

# 4) Chaîne de transfert
kubectl -n audio-external logs deploy/file-mover --tail=200
kubectl -n audio-internal logs deploy/file-puller --tail=200

# 5) Redémarrage ciblé (si nécessaire)
kubectl -n audio-external rollout restart deploy/file-mover
kubectl -n audio-internal rollout restart deploy/file-puller
```

Points à confirmer:
- `file-mover` publie bien la notification interne (pas d'erreur HTTP vers `file-puller`).
- `file-puller` répond `already_pulled` en cas de rejeu (idempotence), sans créer de doublon.
- Les secrets S3 sont présents et identiques dans les namespaces `audio-external` et `audio-internal`.

## Isolation réseau

### Docker Compose (3 réseaux)

| Réseau | Services | Rôle |
|--------|----------|------|
| `external-net` | code-generator, upload-portal, admin-portal, workers, ClamAV, MinIO upload/processed, PostgreSQL ext | Zone DMZ |
| `internal-net` | token-issuer, file-puller, transcription-stub, admin-portal, MinIO internal, PostgreSQL int | Zone interne |
| `dmz-net` | code-generator ↔ token-issuer, file-mover ↔ file-puller | Bridge contrôlé (2 flux seulement) |

### Kubernetes (NetworkPolicies)

```mermaid
flowchart LR
  EXTNS["Namespace audio-external"]
  INTNS["Namespace audio-internal (deny-all par défaut)"]
  CG["code-generator"]
  FM["file-mover"]
  TI["token-issuer:8091"]
  FP["file-puller:8090"]
  INTRA["Trafic intra-zone interne autorisé"]

  EXTNS --- CG
  EXTNS --- FM
  INTNS --- TI
  INTNS --- FP
  INTNS --- INTRA

  CG -->|"Exception 1 autorisée"| TI
  FM -->|"Exception 2 autorisée"| FP
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
| `PULL_REQUEST_TIMEOUT_SECONDS` | `90` | Timeout HTTP (secondes) de `file-mover` vers `file-puller` |
| `DEVICE_TOKEN_RETENTION_HOURS` | `168` | Durée de rétention d'un enrôlement device (zone interne) |
| `DEVICE_REVALIDATE_INTERVAL_SECONDS` | `14400` | Intervalle de revalidation asynchrone des device tokens côté upload |
| `DEVICE_REVALIDATE_MAX_FAILURE_SECONDS` | `14400` | Fenêtre max d'échec backend avant refus des requêtes device |
| `DEVICE_API_PROXY_BASE_URL` | `http://code-generator:8080` | URL du proxy API device utilisé par upload-portal |
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

```mermaid
flowchart TD
  U["Upload mobile"] --> S3U["S3 upload-staging"] --> AV["Scan ClamAV"]
  AV -->|CLEAN| TR["FFmpeg transcode<br/>loudnorm dual-pass -> highpass 80Hz -> lowpass 7kHz -> alimiter<br/>16kHz mono WAV"]
  AV -->|INFECTED| Q["Quarantaine"]
  TR --> QL["Score qualité 1-5"] --> S3P["S3 processed-staging"]
  S3P --> N["NOTIFY"] --> P["PULL"] --> S3I["S3 internal-storage"] --> STT["Transcription STT"]
```

## Formats audio supportés

MP3, WAV, OGG, FLAC, M4A, AAC, WMA, OPUS, WEBM

## Arborescence du projet

```mermaid
flowchart TD
  R["secure-audio-upload/"]
  R --> C["configs/.env.example"]
  R --> D["deploy/"]
  D --> DD["docker/"]
  DD --> DDC["docker-compose.yml"]
  DD --> DDK["keycloak-realm.json"]
  D --> DK["kubernetes/"]
  DK --> DKS["shared/namespaces.yaml + secrets.yaml"]
  DK --> DKE["external-zone/deployments.yaml"]
  DK --> DKI["internal-zone/deployments.yaml"]
  R --> DOC["docs/ARCHITECTURE.md"]
  R --> L["libs/shared/app/ (config, models, DB, S3, queue)"]
  R --> S["services/ (code-generator, upload-portal, workers, token-issuer, file-puller, transcription-stub)"]
  R --> DS["deploy/scripts/setup.sh"]
  R --> DF["deploy/docker/Dockerfile"]
  R --> REQ["requirements.txt"]
  R --> RMD["README.md"]
```

## Licence

Apache-2.0

## Validation enrôlement device

- Cahier de tests: `tests/TEST_PLAN_DEVICE_ENROLLMENT.md`
- Test unitaire token device: `tests/unit/test_device_token.py`
- Scénario simulé: `tests/scenarios/device_enrollment_sequence.sh`
- Synthèse couverture/statut: `tests/TEST_COVERAGE_STATUS.md`
