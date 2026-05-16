# Partage de l'infra dev avec `owuicore-main`

Cet overlay permet de faire tourner `mirai-mesreunions` en réutilisant
le **Keycloak** et le **PostgreSQL** déjà fournis par le socle `owuicore-main`,
au lieu de démarrer leurs équivalents locaux.

## Pourquoi

Sur une machine de dev, `owuicore-postgres-1` (port 5432) et
`owuicore-keycloak-1` (port 8082) sont déjà lancés en permanence. Le compose
standalone de ce repo entre en collision sur ces deux ports.

L'overlay `docker-compose.shared-infra.yml` :

- désactive les services `postgres-external`, `postgres-internal` et `keycloak`
  via `profiles: ["never-start"]` (jamais activé)
- recâble tous les services applicatifs vers `postgres` et `keycloak` du socle
  via le réseau externe `owui-net`
- remap `admin-console` host port 8082 → **8222** (le 8082 est pris par le
  Keycloak owuicore)
- garde tous les autres ports inchangés (8080 mydevices-web, 8081 mobile-upload-pwa,
  8090 internal-ingester, 8091 device-token-authority, 9000-9005 MinIO, 5672/15672 RabbitMQ,
  3310 ClamAV)

## Prérequis à appliquer une seule fois

### 1. Créer les databases dans Postgres owuicore

```bash
docker exec -i owuicore-postgres-1 psql -U owui -d postgres \
  < deploy/docker/bootstrap-shared-db.sql
```

Crée les rôles `audio_ext` et `audio_int` (passwords `audio_ext_dev` /
`audio_int_dev`) + les bases `audio_upload_ext` et `audio_upload_int`.
Idempotent.

### 2. Appliquer les migrations SQL

```bash
# Zone externe
for f in migrations/external/*.sql; do
  docker exec -i owuicore-postgres-1 psql -U audio_ext -d audio_upload_ext < "$f"
done

# Zone interne
for f in migrations/internal/*.sql; do
  docker exec -i owuicore-postgres-1 psql -U audio_int -d audio_upload_int < "$f"
done
```

### 3. S'assurer que le client `mes-reunions` est dans le realm `openwebui` du Keycloak owuicore

Le client `mes-reunions` est déjà déclaré dans
`owuicore-main/keycloak/realm-openwebui.json` et `realm-openwebui.k8s.json`
(persistant, suit le repo owuicore). Aucun nouveau realm à créer côté
mirai-mesreunions — on partage le realm `openwebui` existant et ses
users (eric, patrick, sandrine, etc.).

**Si owuicore est déjà démarré et tournait avec une version antérieure du
realm** (sans le client `mes-reunions`), Keycloak n'auto-importe pas le
delta. Au choix :

**Option A — UI admin Keycloak** (idempotent, le plus simple) :
1. Ouvrir <http://localhost:8082/admin/> (admin + pwd du `.env` owuicore)
2. Sélectionner le realm `openwebui` en haut à gauche
3. Clients → **Create client** → bouton **Import** → choisir
   `keycloak/realm-openwebui.json` (Keycloak ne récupère que le client demandé)
4. OU manuellement : Client ID `mes-reunions`, secret `dev-client-secret`,
   redirectUris `http://localhost:8080/*`, `:8081/*`, `:8222/*` + 127.0.0.1
   équivalents, optional scope `offline_access`

**Option B — kcadm.sh import client** :
```bash
docker exec -i owuicore-keycloak-1 /opt/keycloak/bin/kcadm.sh \
  config credentials --server http://localhost:8080 \
  --realm master --user admin --password <MOT_DE_PASSE_ADMIN>

# Extraction du client mes-reunions depuis le realm.json owuicore
python3 -c "
import json
with open('/Users/etiquet/Documents/GitHub/owuicore-main/keycloak/realm-openwebui.json') as f:
    r = json.load(f)
c = next(c for c in r['clients'] if c['clientId'] == 'mes-reunions')
print(json.dumps(c))
" > /tmp/mes-reunions-client.json

docker cp /tmp/mes-reunions-client.json owuicore-keycloak-1:/tmp/
docker exec -i owuicore-keycloak-1 /opt/keycloak/bin/kcadm.sh \
  create clients -r openwebui -f /tmp/mes-reunions-client.json
```

**Option C — recréer le realm openwebui** (destructif, déconseillé sauf
remise à zéro complète) : drop la DB `keycloak` du Postgres owuicore et
redémarrer Keycloak qui ré-applique `--import-realm` complet.

### 4. (Optionnel) Activer `OIDC_OFFLINE_ACCESS` en dev

Par défaut, l'overlay laisse `OIDC_OFFLINE_ACCESS=false`. Pour matcher prod-bêta
(activé depuis 2026-05-14), il faut aussi assigner le rôle realm
`offline_access` à chaque user owuicore qui doit s'authentifier — sinon
"Offline tokens not allowed" bloque tous les logins (cf
`feedback_oidc_offline_access_keycloak`). À faire dans l'UI admin Keycloak,
realm `openwebui` → Users → édition → Role mapping → assigner `offline_access`.

## Démarrage de la stack mirai-mesreunions

```bash
cd deploy/docker
docker compose \
  -f docker-compose.yml \
  -f docker-compose.shared-infra.yml \
  up -d \
    rabbitmq clamav \
    minio-upload minio-processed minio-internal \
    mydevices-web mobile-upload-pwa admin-console \
    clamav-scanner audio-normalizer dmz-to-internal-bridge \
    device-token-authority internal-ingester transcription-relay
```

Liste explicite des services pour ne pas inclure `postgres-external`,
`postgres-internal` ni `keycloak` (désactivés via profil never-start).

## URLs après démarrage

| Cible          | URL                              |
|----------------|----------------------------------|
| mydevices-web | <http://localhost:8080>          |
| mobile-upload-pwa  | <http://localhost:8081>          |
| admin-console   | <http://localhost:8222> (remap)  |
| internal-ingester    | <http://localhost:8090>          |
| device-token-authority   | <http://localhost:8091>          |
| MinIO upload   | <http://localhost:9001>          |
| MinIO processed| <http://localhost:9003>          |
| MinIO internal | <http://localhost:9005>          |
| RabbitMQ mgmt  | <http://localhost:15672>         |
| Keycloak       | <http://localhost:8082> (socle)  |

## Bascule retour standalone

Si tu veux retomber sur la stack standalone (Postgres + Keycloak dédiés du
compose principal), simplement :

```bash
docker compose -f docker-compose.yml up -d
```

(sans l'overlay). Tu retomberas sur les conflits de port 5432/8082 — il
faudra alors stopper temporairement `owuicore-postgres-1` et `owuicore-keycloak-1`.
