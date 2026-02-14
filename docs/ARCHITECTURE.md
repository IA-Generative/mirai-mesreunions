# Architecture & Sécurité

## Vue d'ensemble

Le système est conçu autour d'un principe de **cloisonnement strict** entre deux zones d'exécution :

### Zone Externe (DMZ)
Exposée à Internet, elle gère l'authentification, la génération de codes, l'upload des fichiers audio, et leur traitement initial (antivirus + transcodage).

### Zone Interne
Isolée du réseau externe, elle stocke les fichiers validés dans les comptes utilisateurs et gère la transcription. **Aucun flux de données ne peut être poussé** depuis l'extérieur vers cette zone.

## Pattern PULL : Sécurité du transfert inter-zones

```
Zone Externe                    DMZ Bridge              Zone Interne
┌──────────────┐               ┌──────────┐            ┌──────────────┐
│ File Mover   │── NOTIFY ────▶│          │── NOTIFY ─▶│ File Puller  │
│              │   (metadata   │ Network  │  (API)     │              │
│              │    seulement) │ Policy   │            │   PULL ──────┼──▶ S3 Processed
│              │               └──────────┘            │   fichier    │    (lecture seule)
└──────────────┘                                       │              │
                                                       │   STORE ─────┼──▶ S3 Internal
                                                       └──────────────┘
```

Le File Mover (zone externe) envoie uniquement des **métadonnées** (ID fichier, utilisateur, nom) via une API authentifiée. Le File Puller (zone interne) **initie le téléchargement** depuis le S3 processed-staging.

Jamais un octet de fichier n'est poussé de l'extérieur vers l'intérieur.

## Pipeline de traitement

```
Upload          Queue         ClamAV        Queue        FFmpeg         Queue
Mobile ───▶ S3 Upload ───▶ AV Worker ───▶ Transcode ───▶ Worker ───▶ File Ready
            Staging         │                              │
            (brut)          ▼ virus?                       ▼
                         Quarantaine                  S3 Processed
                                                     (transcodé)
                                                         │
                                                    NOTIFY ───▶ PULL ───▶ S3 Internal
                                                                          │
                                                                     Transcription
```

## Stockage S3 (3 instances séparées)

| Bucket | Zone | Contenu | Politique |
|--------|------|---------|-----------|
| `upload-staging` | Externe | Fichiers bruts uploadés | Écriture par Upload Portal, lecture par workers |
| `processed-staging` | Externe/Bridge | Fichiers transcodés | Écriture par Transcode Worker, lecture par File Puller (interne) |
| `internal-storage` | Interne | Fichiers validés dans comptes usagers | Écriture par File Puller uniquement |

## Files de messages (RabbitMQ)

| Queue | Producteur | Consommateur | Contenu |
|-------|-----------|-------------|---------|
| `av_scan` | Upload Portal | AV Worker | Demande de scan antiviral |
| `transcode` | AV Worker | Transcode Worker | Fichier sain à transcoder |
| `file_ready` | Transcode Worker | File Mover | Fichier prêt pour transfert |
| `transcription` | File Puller | Transcription Stub | Fichier à transcrire |

Toutes les queues sont **durables** avec **acknowledgement manuel** pour garantir le traitement.

## Authentification & Codes

### Flux OIDC (Code Generator)
1. L'utilisateur se connecte via Keycloak (OIDC Authorization Code Flow)
2. Son identité (`sub`, `email`, `name`) est stockée dans la session Flask
3. Le code-generator appelle le **token-issuer** (zone interne) via API authentifiée
4. Le token-issuer génère le couple (simple_code, qr_token) et l'enregistre en base interne
5. Le code-generator reçoit les tokens et stocke une copie en base externe pour le suivi

### Pourquoi générer côté interne ?
- La zone interne est **l'autorité de confiance** : elle maîtrise l'identifiant de liaison
- La zone externe ne peut pas forger de tokens → pas d'usurpation possible
- Le matching fichier ↔ utilisateur est vérifiable par la zone interne (table `issued_tokens`)
- En cas de compromission de la zone externe, les tokens existants restent valides mais aucun nouveau token frauduleux ne peut être créé

```
Code Generator (ext)                Token Issuer (int)
       │                                    │
       │── POST /api/v1/issue-token ──────▶│
       │   {user_sub, ttl, max_uploads}     │
       │                                    │── generate simple_code
       │                                    │── generate qr_token (crypto)
       │                                    │── INSERT issued_tokens
       │◀── {simple_code, qr_token} ───────│
       │                                    │
       │── INSERT upload_sessions           │
       │   (copie locale pour suivi)        │
       │── Affiche QR + code               │
```

### Code simple & QR Code
- **Code simple** : `ABCDEF` — facile à taper sur mobile
- **QR Code** : URL complète `https://upload.outgate-claude.synchro.fake-domain.name/upload/{qr_token}`
- **Token QR** : 32 bytes URL-safe (cryptographiquement sûr, généré côté interne)
- **TTL configurable** : 15 min → 3 jours
- **Limite d'uploads** : configurable (défaut : 5)

### Matching fichier ↔ utilisateur
Le nom de fichier stocké en S3 porte le code simple : `{CODE}_{uuid}_{filename}.ext`
Le File Puller utilise ce code + les métadonnées de la notification pour router vers le bon compte.
La table `issued_tokens` en zone interne fait foi pour la vérification.

## Transcodage Audio

### Pipeline FFmpeg
```
Input (tout format) → Highpass 80Hz → Lowpass 8kHz → Loudnorm EBU R128 → WAV 16kHz mono
```

- **Bandpass 80-8000 Hz** : Fréquences vocales, élimine bruit basse/haute fréquence
- **Loudnorm** : Normalisation loudness (I=-16 LUFS, TP=-1.5 dB, LRA=11 LU)
- **Sortie** : WAV PCM 16-bit, 16 kHz, mono

### Score de qualité (1-5)
Calculé à partir de :
- Niveau RMS moyen (voix optimale : -25 à -8 dB)
- Ratio silence/signal
- Durée de l'enregistrement
- Taux d'échantillonnage source

## Sécurité Kubernetes

### Network Policies
- **Zone interne** : `deny-all-ingress` par défaut
- **Exception 1** : File Puller accepte le trafic du File Mover (port 8090)
- **Exception 2** : Token Issuer accepte le trafic du Code Generator (port 8091)
- **Zone externe** : Ingress limité aux ports 8080/8081 pour les services frontend

### Isolation réseau Docker Compose
- `external-net` : Services zone externe
- `internal-net` : Services zone interne
- `dmz-net` : Bridge limité pour les 2 flux inter-zones :
  - Code Generator (ext) → Token Issuer (int) : demande de token
  - File Mover (ext) → File Puller (int) : notification de fichier prêt
