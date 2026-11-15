# QR Enrollment Portal

Générateur de QR code d'enrôlement d'appareil, **derrière Passage2**.

L'agent ouvre le portail sur son ordinateur. Passage2 l'authentifie, il génère un QR code et le scanne avec son téléphone : l'app mobile (*MIrAI Audio Secure*, bouton « Scanner le QR code ») ou la PWA d'upload. Les enregistrements envoyés par cet appareil arrivent dans **son espace mes-réunions**, comme avec un QR généré depuis l'onglet « Appareils » de mes-réunions.

Le service est autonome. Il ne dépend ni de `mesreunions-web` ni de sa session OIDC, et peut être exposé derrière n'importe quel reverse proxy Passage2.

## Parcours

```
Navigateur ─▶ Passage2 (Apache + mellon, SAML)
                 │  en-têtes Mellon-NameID / Mellon-Groups + secret proxy
                 ▼
          qr-enrollment-portal ──▶ Keycloak (admin API) : e-mail → sub
                 │
                 ├──▶ device-token-authority /api/v1/issue-token (qr_token, code court)
                 └──▶ base externe : UploadSession (rattache le qr_token au sub)
                 ▼
          QR = https://<dmz>/upload/<qr_token>  ─▶  app mobile / PWA
```

Le cœur (émission du jeton, écriture de la session, liste des appareils) est partagé avec `mesreunions-web` dans `libs/shared/app/enrollment.py`. Seule la source d'identité diffère.

## Fonctions

- `GET /` : page de génération du QR et liste « Mes appareils ».
- `POST /api/generate-code` : `{ttl_minutes?, max_uploads?}` → QR (PNG en data URI), code court, URL, expiration. Limité à 10 QR par minute et par agent.
- `GET /api/my-devices` : appareils de l'agent, sans `device_key` ni empreintes.
- `POST /api/my-devices/<id>/rename` | `/revoke`, `POST /api/my-devices/revoke-all`.
- `GET /healthz` : sans authentification.

Les requêtes qui modifient l'état exigent l'en-tête `X-Requested-With: qr-enrollment-portal` (protection CSRF).

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `PASSAGE2_PROXY_SECRET` | — | Secret ajouté par le proxy (32 caractères minimum). |
| `PASSAGE2_PROXY_SECRET_HEADER` | `X-Passage2-Proxy-Secret` | En-tête qui porte ce secret. |
| `PASSAGE2_TRUSTED_PROXIES` | — | CIDR autorisés à appeler le service, séparés par des virgules. |
| `PASSAGE2_USER_HEADER` | `Mellon-NameID` | Identifiant de l'agent. |
| `PASSAGE2_EMAIL_HEADER` | `Mellon-NameID` | Adresse e-mail (ex. `Mellon-mail` si le NameID n'est pas l'e-mail). |
| `PASSAGE2_NAME_HEADER` | — | Nom affiché (optionnel). |
| `PASSAGE2_GROUPS_HEADER` | `Mellon-Groups` | Groupes, séparés par `,` ou `;`. |
| `PASSAGE2_REQUIRED_GROUPS` | — | Si renseigné, l'agent doit appartenir à l'un de ces groupes. |
| `PASSAGE2_DEV_IDENTITY` | — | E-mail simulé, **uniquement** avec `ENVIRONMENT=development`. |
| `KEYCLOAK_BASE_URL` / `KEYCLOAK_REALM` | `http://keycloak:8080` / `openwebui` | Realm des comptes mes-réunions. |
| `KEYCLOAK_ADMIN_CLIENT_ID` / `_SECRET` | — | Client à compte de service (voir plus bas). |
| `QR_UPLOAD_BASE_URL` | `UPLOAD_PORTAL_BASE_URL` | URL publique de la DMZ encodée dans le QR. |
| `TOKEN_ISSUER_API_URL`, `TOKEN_ISSUER_INTERNAL_BASE_URL`, `INTERNAL_API_TOKEN` | — | Accès à device-token-authority, comme pour mesreunions-web. |
| `EXT_DB_*` | — | Base externe (tables `upload_sessions`, `upload_token_options`). |
| `CODE_TTL_MINUTES`, `CODE_TTL_MAX_MINUTES`, `MAX_UPLOADS_PER_SESSION`, `AUTO_TRANSCRIBE_POLICY` | config partagée | Mêmes plafonds que mes-réunions. |

**Le service refuse de démarrer** hors développement si aucun mécanisme de confiance n'est configuré (`PASSAGE2_PROXY_SECRET` et/ou `PASSAGE2_TRUSTED_PROXIES`), si le secret est faible, ou si `PASSAGE2_DEV_IDENTITY` est défini.

## Côté Passage2 (Apache)

Le proxy doit **écraser** les en-têtes d'identité (`RequestHeader set`, jamais `append`) et ajouter le secret :

```apache
RequestHeader set Mellon-NameID %{MELLON_NAME_ID}e
RequestHeader set Mellon-Groups %{MELLON_groups}e
RequestHeader set X-Passage2-Proxy-Secret "<PASSAGE2_PROXY_SECRET>"
ProxyPass        / http://qr-enrollment-portal:8093/
ProxyPassReverse / http://qr-enrollment-portal:8093/
```

Le service ne doit être joignable que par le proxy : NetworkPolicy, pas d'Ingress direct. Hors ministère, le bouchon Passage2 du chart `cloud-pi-native/helm-projects-mocks` reproduit ce montage.

## Côté Keycloak

Créer dans le realm `openwebui` un client **confidentiel** `qr-enrollment-portal`, avec *Service accounts* activé et *Standard flow* désactivé. Lui attribuer le seul rôle client `realm-management` → `view-users`. Renseigner son secret dans `KEYCLOAK_ADMIN_CLIENT_SECRET`.

La correspondance se fait sur l'e-mail exact d'un compte **actif**. Il y a trois cas d'échec :
- aucun compte : l'agent est invité à se connecter une première fois à mes-réunions ;
- plusieurs comptes avec la même adresse : l'agent doit contacter le support ;
- Keycloak injoignable : erreur 503.

## Local

```bash
cd deploy/docker
QR_PORTAL_KEYCLOAK_CLIENT_SECRET=<secret du client> docker compose up qr-enrollment-portal
# http://localhost:8093 — identité simulée : PASSAGE2_DEV_IDENTITY (test@example.com)
```

## Tests

```bash
python -m pytest tests/unit/test_passage2.py tests/unit/test_keycloak_users.py \
  tests/unit/test_enrollment.py tests/unit/test_qr_enrollment_portal.py
```
