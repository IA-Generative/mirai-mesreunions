# Cahier de Tests - Enrôlement Device Navigateur

## Objectif
Valider le cycle complet d'enrôlement persistant navigateur autour du QR/token:
- enrôlement initial,
- persistance locale du token device,
- fast-path de validation externe,
- revalidation asynchrone,
- révocation user/admin,
- blocage explicite après fenêtre d'échec backend.

## Pré-requis
- Stack démarrée (`docker compose` ou Kubernetes).
- OIDC fonctionnel.
- Un compte utilisateur de test.

## Tests unitaires
1. Signature/validation de token device:
   - fichier: `tests/unit/test_device_token.py`
   - attendu: token valide accepté, token modifié rejeté, secret incorrect rejeté.

## Scénario simulé E2E
1. Générer un QR via le `code-generator`.
2. Ouvrir l'URL upload depuis un navigateur "nouveau device".
3. Vérifier qu'un enrôlement est effectué automatiquement (`/api/device/enroll/...`).
4. Vérifier que le token device est stocké en localStorage (`upload_device_token:<qr_token>`).
5. Uploader un audio:
   - attendu: `/api/upload/<qr_token>` accepte avec header `X-Device-Token`.
6. Recharger la page upload:
   - attendu: le token persistant est réutilisé, pas de nouvel enrôlement.
7. Révoquer le device depuis l'interface QR interne:
   - attendu: upload/status retourne `401` avec message de rescanner/régénérer.
8. Révoquer tous les devices depuis l'admin:
   - attendu: tous les devices passent `revoked` et les requêtes device échouent.
9. Simuler indisponibilité backend validation:
   - attendu: retry asynchrone pendant la fenêtre configurée (défaut 4h),
   - puis blocage explicite au-delà.

## Vérifications UI
1. Upload:
   - message PWA visible,
   - message explicite si token expiré/révoqué.
2. QR interne:
   - liste devices,
   - renommage,
   - révocation unitaire,
   - révocation globale.
3. Admin:
   - liste devices,
   - révocation unitaire,
   - révocation globale.
