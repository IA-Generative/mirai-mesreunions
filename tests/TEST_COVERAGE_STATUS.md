# Synthèse Couverture Tests - Enrôlement Device

## Portée
- Enrôlement persistant navigateur.
- Validation fast-path + asynchrone.
- Révocation user/admin.
- UX de guidage en cas de token invalide/révoqué.

## Statut
- Exécuté (Docker Compose + Kubernetes), corrections appliquées.

## Couverture actuelle
- Unitaires:
  - `tests/unit/test_device_token.py`
- Scénario simulé:
  - `tests/scenarios/device_enrollment_sequence.sh`
- Cahier de validation:
  - `tests/DISCOVERY_TEST_PLAN.md`

## Résultats
- Unitaires:
  - `pytest -q tests/unit/test_device_token.py` → **3 passed**
- Smoke enrôlement (Docker Compose):
  - génération token interne + insertion session externe de test
  - bootstrap sans token device → `needs_enrollment`
  - enrôlement device → `device_token` délivré
  - bootstrap avec token device → `enrolled`
- Smoke révocation (Docker Compose):
  - révocation unitaire backend
  - bootstrap suivant avec token révoqué → `401` + message explicite (bloqué)
- Déploiement Kubernetes:
  - image push tag `20260215-142003` (amd64)
  - rollouts validés: `admin-portal`, `code-generator`, `upload-portal`, `antivirus-worker`,
    `transcode-worker`, `file-mover`, `token-issuer`, `file-puller`, `transcription-stub`
  - état final pods: **Running/Ready** dans `audio-external` et `audio-internal`

## Anomalies rencontrées et corrigées
- Révocation backend non bloquante immédiatement (acceptation fast-path jusqu'au prochain cycle de revalidation):
  - correction: validation backend forte à chaque bootstrap `/api/device/session/<qr_token>`
  - effet: révocation/expiration détectée rapidement et accès bloqué.
- Déploiement K8s avec image `arm64` sur nœuds `amd64` (`exec format error`):
  - correction: rebuild/push image `amd64`, puis rollout sur tag dédié.

## Limites restantes
- Le scénario `tests/scenarios/device_enrollment_sequence.sh` requiert un `QR_TOKEN` valide injecté via env.
- Les validations OIDC/UX navigateur restent à confirmer manuellement (non couvertes en pur CLI).
