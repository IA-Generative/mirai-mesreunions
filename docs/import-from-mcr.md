# Import de réunions depuis MCR (sens pull)

Pendant de [`integrate-with-mcr.md`](./integrate-with-mcr.md) qui décrit le **push** mesreunions → MCR. Ce document décrit le sens inverse : un utilisateur clique « 📥 Depuis MCR » dans `mesreunions.fake-domain.name`, sélectionne une ou plusieurs réunions hébergées sur `compte-rendu.mirai.fake-domain.name`, et le système les importe (audio + transcription).

## Vue d'ensemble

```
   ┌────────────┐
   │  Browser   │ 1. Click "📥 Depuis MCR"
   └─────┬──────┘
         │ /api/mcr/meetings   (cookie OIDC mesreunions-web)
         ▼
┌───────────────────┐    2. fetch refresh_token chiffré
│   mesreunions-web │ ─────►  device-token-authority (GET /api/v1/oidc-refresh-fetch/{sub})
└─────┬──────┬──────┘
      │      │                                ┌──────────────┐
      │      │ 3. refresh → access            │  sso.mirai   │
      │      └────────────────────────────────►              │
      │                                       │  realms/mirai│
      │                                       └──────────────┘
      │ 4. GET /api/meetings/?page=1 (Bearer)
      ▼
┌────────────────────────────────┐
│  compte-rendu.mirai (gateway)  │
└────────────────────────────────┘

   ┌────────────┐
   │  Browser   │ 5. User coche N → POST /api/mcr/import
   └─────┬──────┘                  { meeting_ids:[…], fallback_transcript:true }
         ▼
┌───────────────────┐
│   mesreunions-web │ 6. INSERT N rows user_audio_files
│                   │    (origin='mcr_import', status='mcr_import_pending')
│                   │ 7. publish RabbitMQ QUEUE_MCR_IMPORT (N msgs)
└───────────────────┘

                                 ┌─────────────────────┐
QUEUE_MCR_IMPORT  ──────────────►│ internal-ingester   │
                                 │ (thread mcr_importer)│
                                 └─────────┬───────────┘
                                           │ 8. refresh→access (sso.mirai)
                                           │ 9. GET /meetings/{id}/audio    ─► S3 audio-internal
                                           │    UPDATE row status='pending'
                                           │    (le pipeline kevent prend la suite)
                                           │
                                           │ 10. (si 404 audio + fallback)
                                           │    POST /meetings/{id}/transcription
                                           │    → DOCX → python-docx extract
                                           │    UPDATE row transcription_text +
                                           │    status='mcr_transcript_only'
                                           ▼
                                       user_audio_files
```

## API MCR consommée

Auth : `Authorization: Bearer <access_token>` (JWT realm `mirai`, client `mes-reunions`).

| Méthode | Path | Description |
|---------|------|-------------|
| `GET`  | `/api/meetings/?search=&page=&page_size=`           | Liste paginée user-scoped |
| `GET`  | `/api/meetings/{meeting_id}/audio`                  | Stream binaire `audio/webm` |
| `POST` | `/api/meetings/{meeting_id}/transcription`          | Stream DOCX (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`) |

Pas de flag `has_audio` / `has_transcript` dans la liste → on tente le GET et on retombe sur le transcript en 404.

Source officielle des endpoints : monorepo MCR `mcr-gateway/mcr_gateway/app/api/meeting_router.py` (réf locale `/Users/etiquet/Documents/GitHub/mcr/`).

## API mesreunions-web

### `GET /api/mcr/meetings`

Relai paginé vers MCR. Auth : cookie de session OIDC.

Query :
- `page` (défaut 1)
- `page_size` (défaut 20, max 50)
- `search` (optionnel)

Réponse 200 (forwardée brute depuis MCR) :
```json
{
  "total_items": 42,
  "total_pages": 3,
  "page": 1,
  "data": [
    { "id": 17, "name": "Réunion 5 mai 2026",
      "status": "completed", "start_date": "2026-05-05T10:00:00Z",
      "creation_date": "2026-05-05T10:01:23Z", "name_platform": "Webex",
      "url": "...", "notes": "...", "end_date": "2026-05-05T11:30:00Z" },
    …
  ]
}
```

Codes d'erreur applicatifs :
- `401 {"error":"auth_required_reconnect"}` — refresh_token absent ou expiré.
- `403 {"error":"mcr_forbidden"}` — MCR refuse le token (audience invalide ou user pas autorisé).
- `502 {"error":"sso_unreachable"|"mcr_unreachable"|"mcr_5xx:NNN"}` — backend down.

### `POST /api/mcr/import`

Body :
```json
{ "meeting_ids": ["17", "42"], "fallback_transcript": true }
```

- `meeting_ids` : 1 à 50 IDs MCR.
- `fallback_transcript` : si true et qu'une réunion n'a pas d'audio, on récupère la transcription DOCX à la place.

Réponse 202 :
```json
{ "accepted": true, "import_ids": ["uuid1", "uuid2"] }
```

Les IDs renvoyés sont ceux des nouvelles lignes `user_audio_files`. Le front polle `/api/my-files` pour suivre l'évolution (`transcription_status` passe de `mcr_import_pending` → `pending` → `kevent_*` pour les imports audio, ou directement à `mcr_transcript_only` pour les imports sans audio).

Dédoublonnage : si l'utilisateur ré-clique « Importer » pour une réunion déjà importée, on réutilise la ligne existante (l'index partiel migration 019 garantit l'unicité). Si la précédente tentative a échoué, on relance le worker.

## Statuts dans `user_audio_files.transcription_status`

| Statut | Sens |
|--------|------|
| `mcr_import_pending` | Ligne créée, message publié sur la queue, worker pas encore traité. |
| `pending` | Audio téléchargé et stocké en S3, le pipeline (kevent) va prendre la main. |
| `mcr_transcript_only` | Pas d'audio sur MCR, transcription DOCX importée comme texte. |
| `mcr_import_failed` | Échec terminal (auth, applicatif). UI peut proposer un re-clic. |

## Composants

| Composant | Fichier |
|-----------|---------|
| Helper Mirai OIDC (refresh → access) | [`libs/shared/app/mirai_oidc.py`](../libs/shared/app/mirai_oidc.py) |
| Client HTTP MCR (push + pull) | [`services/dmz-to-internal-bridge/app/mcr_client.py`](../services/dmz-to-internal-bridge/app/mcr_client.py) |
| Blueprint mesreunions-web | [`services/mesreunions-web/app/modules/mcr_import/routes.py`](../services/mesreunions-web/app/modules/mcr_import/routes.py) |
| Worker consumer | [`services/dmz-to-internal-bridge/app/mcr_importer.py`](../services/dmz-to-internal-bridge/app/mcr_importer.py) |
| Modale UI | [`services/mesreunions-web/frontend/tabs/meetings.js`](../services/mesreunions-web/frontend/tabs/meetings.js) (`_openMcrImportModal`) |
| Migration DB | [`migrations/internal/019_user_audio_files_origin.sql`](../migrations/internal/019_user_audio_files_origin.sql) |
| Queue déclarée | `QUEUE_MCR_IMPORT="mcr_import"` dans [`libs/shared/app/queue_helper.py`](../libs/shared/app/queue_helper.py) |

## Configuration Keycloak côté Mirai

Le client `mes-reunions` (importé dans `sso.mirai/realms/mirai`) doit avoir :

- `redirectUris` incluant `https://mesreunions.fake-domain.name/*`
- Scope `offline_access` attaché en *Default* ou *Optional*
- Rôle realm `offline_access` dans les Default Roles du realm `mirai`

Sans ça, le login casse (cf. memo `OIDC offline_access — 2 étapes Keycloak`).

## Modes d'échec connus

1. **`401 auth_required_reconnect`** — l'utilisateur n'a pas de refresh_token capturé : il s'est loggué avant l'activation de `OIDC_OFFLINE_ACCESS=true`, ou son refresh a expiré côté KC. Solution : se déconnecter / reconnecter pour réémettre un offline token.
2. **`403 mcr_forbidden`** — l'access_token est valide vis-à-vis de Mirai mais MCR ne l'accepte pas (audience). À investiguer côté admin SSO Mirai si ça arrive ; potentiellement requérir un token-exchange.
3. **Audio en webm corrompu** — l'audio-normalizer (ffmpeg) gère webm nativement ; si un fichier rejette, vérifier le `Content-Type` côté MCR et les logs ffmpeg.
4. **DOCX vide** — `python-docx` n'extrait que les paragraphes ; si la transcription est dans une table ou un commentaire, l'extraction renvoie une chaîne vide. À enrichir si le cas se présente (ajouter parsing tables).

## Verification end-to-end (smoke prod-bêta)

1. Login `https://mesreunions.fake-domain.name/` avec un user qui possède une réunion sur compte-rendu.mirai.
2. Onglet « Mes réunions » → cliquer 📥 Depuis MCR → la modale doit lister les réunions du user.
3. Cocher 1 réunion qui a un audio + 1 sans audio (si dispo).
4. Vérifier en DB :
   ```sql
   SELECT id, transcription_status, mcr_meeting_id, origin
     FROM user_audio_files WHERE origin='mcr_import' ORDER BY created_at DESC LIMIT 5;
   ```
5. Vérifier la liste dans l'UI : les deux entrées apparaissent ; l'une passe progressivement de `mcr_import_pending` → `pending` → `kevent_*` ; l'autre passe directement à `mcr_transcript_only`.
