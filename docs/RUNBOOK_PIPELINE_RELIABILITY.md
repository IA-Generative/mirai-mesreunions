# Runbook — Fiabilité du pipeline transcription

Procédures opérationnelles pour le pipeline `user_audio_files` →
Kevent → résultat utilisateur, post-deploy des commits de
fiabilisation (mai 2026, branche `feat/import-from-mcr`).

Contexte produit dans
[`docs/adr/0001-pipeline-liveness-vs-progress.md`](adr/0001-pipeline-liveness-vs-progress.md).

## 1. Déploiement initial (à faire une fois)

L'ordre est **strict** : migration SQL AVANT rollout pod, sinon le
schema-cache SQLAlchemy renvoie "UndefinedColumn" en boucle.

```bash
# 1) Migration 020 sur la DB internal
kubectl exec -n audio-internal postgres-internal-0 -- \
  psql -U audio_int -d audio_upload_int -f - \
  < migrations/internal/020_user_audio_last_error.sql

# 2) Vérifier
kubectl exec -n audio-internal postgres-internal-0 -- \
  psql -U audio_int -d audio_upload_int -c \
  "\d user_audio_files" | grep last_error

# 3) Build + push (cycle ~3-4 min/service)
bash scripts/commit-push-build.sh internal-ingester \
                                  dmz-to-internal-bridge \
                                  mesreunions-web

# 4) Rollout via kustomize (JAMAIS kubectl apply -f direct)
kustomize build --load-restrictor=LoadRestrictionsNone \
  deploy/kubernetes/environments/prod-beta/internal/ \
  | kubectl apply -f -
kustomize build --load-restrictor=LoadRestrictionsNone \
  deploy/kubernetes/environments/prod-beta/external/ \
  | kubectl apply -f -

# 5) Vérifier les 4 internal-ingester ont redémarré
kubectl get pods -n internal -l app=internal-ingester -o wide

# 6) Vérifier le watchdog ne démarre QU'1× par pod (flock Phase 4b)
for pod in $(kubectl get pods -n internal -l app=internal-ingester \
                              -o name); do
  echo "=== $pod ==="
  kubectl logs -n internal $pod | grep -c "pipeline_watchdog started"
done
# Attendu : 1 par pod (avant le fix : 2)

# 7) Communiquer aux utilisateurs un hard refresh PWA
#    (modif frontend meetings.js — service worker agressif)
```

## 2. Vérification end-to-end après deploy

### 2a. Heartbeat Kevent fonctionnel (Phase 6)

Sur un audio long (> 5 min) déjà uploadé, vérifier dans les logs
ingester que le poll Kevent loggue à chaque tick :

```bash
kubectl logs -n internal -l app=internal-ingester --tail=500 \
  | grep -E "Kevent GET /jobs/audio/" | head -20
```

Attendu : ~1 ligne toutes les 3s pendant toute la durée du poll
(KEVENT_ASYNC_POLL_INTERVAL_SECONDS=3.0). Si silence > 10s sur un
job en cours = régression.

En DB, sur une row en cours de traitement :

```sql
SELECT id, transcription_status, last_activity_at,
       EXTRACT(EPOCH FROM (NOW() - last_activity_at)) AS staleness_s
  FROM user_audio_files
 WHERE transcription_status IN ('kevent_processing','kevent_transcribing')
 ORDER BY last_activity_at DESC;
```

Attendu : `staleness_s < 10` tant que le job tourne. Si > 60s = le
heartbeat ne fire pas, investiguer.

### 2b. Rows historiques bloquées

```sql
-- État avant intervention utilisateur
SELECT id, origin, transcription_status, reprocess_version,
       last_error_kind, last_error_message
  FROM user_audio_files
 WHERE transcription_status IN ('kevent_failed','mcr_import_failed')
   AND reprocess_version > 10
 ORDER BY reprocess_version DESC LIMIT 20;
```

Action utilisateur :
1. Ouvrir l'onglet "Mes réunions" sur `mesreunions.fake-domain.name`.
2. Cliquer **"Relancer les sujets bloqués"**.
3. Observer la bannière verte de confirmation.

Observation côté ops, dans les 5 min qui suivent :

```bash
kubectl logs -n internal -l app=internal-ingester --tail=200 \
  | grep -E "watchdog|resume|reprocess_version"
```

Pour chaque row relancée, attendu :
1. `pipeline_watchdog tick: scanned=N claimed=M skipped=K` (M ≥ 1).
2. `Kevent submitted: job_id=… service_type=audio operation=transcription`.
3. Pour chaque poll, `Kevent GET /jobs/audio/<id> → 200` toutes les 3s.
4. Convergence vers `kevent_completed` (ou `kevent_partially_completed`,
   ou statut terminal avec `last_error_kind` peuplé).
5. **PAS** de re-republish du même `audio_id` toutes les 5 min comme
   avant le fix.

### 2c. Rows avec audio S3 purgé

```sql
SELECT id, stored_filename, last_error_kind, last_error_message
  FROM user_audio_files
 WHERE last_error_kind = 's3_object_purged';
```

Pour ces rows : le bouton "Relancer" est silencieusement ignoré par
le watchdog (Phase 5 exclude rule). L'UI affiche "supprimer la
ligne". L'utilisateur supprime via le bouton trash → soft-delete via
`trashed_at` (cf memory `project_trash_soft_delete`).

## 3. Détection des régressions

### Signal #1 — Re-submits abusifs

```sql
-- Rows ayant été republiées > 3x en 24h depuis le déploiement
SELECT id, reprocess_version, last_activity_at,
       reprocess_history->-1->>'type' AS last_trigger
  FROM user_audio_files
 WHERE last_activity_at > NOW() - INTERVAL '24 hours'
   AND reprocess_version > 3
 ORDER BY reprocess_version DESC;
```

Si plus de 5 rows par jour avec `reprocess_version` > 3 et
`last_trigger='watchdog'` : régression possible du heartbeat. Vérifier
les logs ingester pour confirmer.

### Signal #2 — Rows coincées > 1h sans progrès

```sql
SELECT id, transcription_status, last_activity_at,
       EXTRACT(EPOCH FROM (NOW() - last_activity_at))/3600 AS hours_stuck
  FROM user_audio_files
 WHERE transcription_status NOT IN (
         'kevent_completed', 'kevent_partially_completed',
         'kevent_failed', 'mcr_import_failed', 'mcr_pushed',
         'mcr_unavailable_on_source')
   AND last_activity_at < NOW() - INTERVAL '1 hour'
 ORDER BY hours_stuck DESC;
```

Attendu post-fix : 0 row. Si > 0, c'est soit (a) un pod en train
de traiter un audio géant (légitime, vérifier durée audio vs RTF
attendu), soit (b) une régression.

### Signal #3 — Doublons watchdog par pod

```bash
for pod in $(kubectl get pods -n internal -l app=internal-ingester -o name); do
  count=$(kubectl logs -n internal $pod | grep -c "pipeline_watchdog started")
  if [ "$count" -ne "1" ]; then
    echo "RÉGRESSION : $pod a $count watchdog (attendu : 1)"
  fi
done
```

Si > 1 : le flock POSIX (Phase 4b) ne tient pas. Vérifier
`PIPELINE_WATCHDOG_LOCK_PATH` (défaut `/tmp/pipeline_watchdog.lock`)
n'est pas sur un volume partagé entre pods (sinon LOCK_NB échoue
entre pods, pas seulement entre workers).

## 4. Rollback

Si le déploiement casse :

```bash
# 1) Revert au commit précédent
git revert --no-edit 78abecb 6c63c35 eb6be55
git push origin feat/import-from-mcr

# 2) Re-build + re-rollout
bash scripts/commit-push-build.sh internal-ingester \
                                  dmz-to-internal-bridge \
                                  mesreunions-web

# 3) La migration 020 est BACKWARD COMPATIBLE (ALTER TABLE ADD COLUMN
#    avec IF NOT EXISTS, sans valeur required) → pas besoin de
#    rollback côté DB. Les colonnes last_error_* restent en DB inutilisées.
```

## 5. Quoi faire si Kevent commence à retourner `abandoned_by_client`

(Évolution future, cf
[`docs/upstream-asks/kevent/03-feature-lease-based-job-cancellation.md`](upstream-asks/kevent/03-feature-lease-based-job-cancellation.md))

Pas déployé aujourd'hui — ne deviendra pertinent que quand Kevent
expose le lease-based cancellation. Quand ce sera le cas :

1. Vérifier que `KEVENT_ASYNC_POLL_INTERVAL_SECONDS=3.0` < `JOB_LEASE_SECONDS`
   côté gateway (la marge doit être > 5× pour absorber les jitter
   réseau).
2. Si retours `abandoned_by_client` apparaissent dans
   `last_error_message` → c'est un poll qui a manqué la deadline.
   Investiguer la latence réseau ingester → gateway.

## 6. Métriques à exposer (à venir)

Pas de métriques Prometheus dédiées à ce jour. Backlog :

- `pipeline_watchdog_scan_total{result}` (claimed | skipped)
- `pipeline_watchdog_steal_total` (rows reprises alors qu'un poll
  Kevent était actif — devrait être 0 post-fix)
- `kevent_poll_heartbeat_age_seconds` (age max de `last_activity_at`
  sur les rows en `kevent_processing`)
- `user_audio_files_terminal_by_kind{kind}` (compteur par
  `last_error_kind` distinct)

Ces métriques transforment le runbook en alerting automatique. Sortir
dans un sprint dédié observabilité.
