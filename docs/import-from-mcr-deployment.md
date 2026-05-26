# Déploiement prod-bêta — feature `import-from-mcr`

Runbook complet pour basculer la branche `feat/import-from-mcr` en prod-bêta interne.

## Pré-vol

```bash
# 1. Tests passent (déjà fait au build de la branche)
python3 -m pytest tests/unit/ -q          # → 503 passed
python3 -m pytest tests/regression/ -q    # → 41 passed (+107 skipped)

# 2. Branche à jour avec main
git fetch origin
git rebase origin/main   # rebase si main a avancé depuis le branchage

# 3. Vérifier l'absence de fichier en local non-committé
git status               # → clean
```

## Étape 1 — Push branche + cloud build

```bash
git push -u origin feat/import-from-mcr

# Cloud build amd64 (~5-8 min) + push image SCW registry
# REMOTE_HOST doit être exporté (memo reference_commit_push_build_script)
deploy/scripts/commit-push-build.sh
```

À surveiller dans la sortie : `Successfully tagged ... :latest` puis `Successfully pushed`. Tail le log explicitement (memo : SSH reset = exit 0 faux positif).

## Étape 2 — Migration DB AVANT rollout

> 🚨 Critique (memo `migration_before_rollout`) : appliquer la migration **avant** le rollout. Sinon SQLAlchemy → "UndefinedColumn" en boucle sur `user_audio_files.origin` et il faudra re-rollout pour reset le pool.

```bash
export KUBECONFIG=/Users/etiquet/Documents/GitHub/mirai-mesreunions/deploy/kubernetes/kubeconfigs/kubeconfig-internal-gw.yaml

# Connexion à postgres-internal et exécution
kubectl -n audio-internal exec -i $(kubectl -n audio-internal get pod -l app=postgres-internal -o name | head -1) -- \
  psql -U audio_int -d audio_upload_int \
  < migrations/internal/019_user_audio_files_origin.sql

# Vérifier que la colonne existe
kubectl -n audio-internal exec -i $(kubectl -n audio-internal get pod -l app=postgres-internal -o name | head -1) -- \
  psql -U audio_int -d audio_upload_int -c "\d user_audio_files" | grep origin
# → origin | character varying(20) | not null | 'upload'::character varying

# Vérifier l'index partiel
kubectl -n audio-internal exec -i $(kubectl -n audio-internal get pod -l app=postgres-internal -o name | head -1) -- \
  psql -U audio_int -d audio_upload_int -c "\d user_audio_files" | grep mcr_import_uniq
# → "idx_user_audio_files_mcr_import_uniq" UNIQUE, btree (user_sub, mcr_meeting_id) WHERE …
```

## Étape 3 — Rollout overlay kustomize

```bash
# TOUJOURS via kustomize --load-restrictor (memo manifest_prod_divergence)
kustomize build --load-restrictor=LoadRestrictionsNone \
  deploy/kubernetes/environments/prod-beta/internal/ | kubectl apply -f -

# Forcer le rollout des pods pour qu'ils chargent le nouveau code
kubectl -n audio-internal rollout restart \
  deployment/mesreunions-web \
  deployment/admin-console \
  deployment/internal-ingester

# Suivre
for d in mesreunions-web admin-console internal-ingester; do
  echo "=== $d ==="
  kubectl -n audio-internal rollout status deployment/$d --timeout=180s
done
```

## Étape 4 — Smoke test fonctionnel

```bash
# Logs au boot — vérifier que les workers démarrent
kubectl -n audio-internal logs deploy/internal-ingester --tail=50 | grep -Ei "mcr_importer|pipeline_watchdog"
# Attendu :
#   "mcr_importer: starting consumer on mcr_import"
#   "Declared queue: mcr_import"

# Pas d'erreur SQLAlchemy
kubectl -n audio-internal logs deploy/mesreunions-web --tail=50 | grep -iE "error|exception|undefinedcolumn"
# → rien
```

Puis dans le navigateur :

1. Ouvrir `https://mesreunions.fake-domain.name/` en navigation privée.
2. Login Mirai (`mes-reunions` client, realm `mirai`).
3. Onglet « Mes réunions » → cliquer **📥 Depuis MCR**.
4. La modale doit lister les réunions du user. Si :
   - "Reconnecte-toi…" → le user n'a pas de refresh_token capturé (loggé avant offline_access ou refresh expiré). Solution : déconnexion / reconnexion.
   - "Ton compte n'a pas accès…" → MCR rejette l'access_token. Investiguer l'audience JWT côté admin SSO Mirai.
   - Erreur HTTP 5xx → cf logs `kubectl -n audio-internal logs deploy/mesreunions-web -f`.
5. Cocher 1 réunion avec audio + 1 sans audio (si dispo), valider.
6. Vérifier en DB :
   ```bash
   kubectl -n audio-internal exec -i $(kubectl -n audio-internal get pod -l app=postgres-internal -o name | head -1) -- \
     psql -U audio_int -d audio_upload_int -c \
     "SELECT id, transcription_status, mcr_meeting_id, origin, transcription_text IS NOT NULL AS has_txt \
        FROM user_audio_files WHERE origin='mcr_import' ORDER BY created_at DESC LIMIT 5;"
   ```
   → on doit voir les rows passer de `mcr_import_pending` → `pending` → (puis le pipeline kevent prend la suite) → `kevent_completed` pour le cas audio, et `mcr_transcript_only` (has_txt=t) directement pour le cas DOCX-seul.
7. Recharger `mesreunions.fake-domain.name` → les 2 réunions doivent apparaître dans la liste « Mes réunions » normales.

## Rollback

Si bascule à problèmes (login cassé, erreurs en chaîne) :

```bash
# Re-déployer le commit précédent main (33746f0 = bascule SSO Mirai)
git checkout main
kustomize build --load-restrictor=LoadRestrictionsNone \
  deploy/kubernetes/environments/prod-beta/internal/ | kubectl apply -f -
kubectl -n audio-internal rollout restart deployment/mesreunions-web deployment/admin-console deployment/internal-ingester
```

La migration 019 (ajout de colonne `origin` + index partiel) est **NON destructive** : pas besoin de rollback DB. Les rows `mcr_import` existantes restent en place ; le bouton UI disparaîtra simplement.

## Suites post-déploiement

À planifier ensuite :

- **Audience JWT** : confirmer que `mes-reunions` access_tokens passent l'audience check côté MCR. Si non, ticket admin Mirai.
- **Tests d'intégration** pour le worker `mcr_importer` (le handler n'a que des tests unitaires des couches HTTP, pas du flow complet — DB + S3 nécessaires).
- **Webm → m4a** : si l'ingester tombe sur des erreurs ffmpeg, ajouter un step de normalisation explicite avant le PUT S3.
- **UI badge « importé de MCR »** : différencier visuellement les rows `origin='mcr_import'` dans `tabs/meetings.js`.
- **Reprise après échec** : depuis l'UI, exposer un bouton « Réessayer l'import » sur les rows `mcr_import_failed`.
