# RECOMMENDATION — diarization-api

## Décision

**Activer en production la piste 2 (pré-chargement du waveform en tensor)**, et **changer le défaut** du flag `DIARIZATION_PRELOAD_WAVEFORM` à `true` une fois la PR mergée et un canary observé sans régression.

Aucune autre piste n'apporte de gain significatif sur ce pipeline. La piste 3 (fp16) reste un follow-up envisageable mais non bloquant.

## Pourquoi

| Métrique | Avant | Après | Impact opérationnel |
|----------|-------|-------|---------------------|
| Wall-clock 13 min audio | 115 s | **24 s** | latence utilisateur ÷ 4.8 |
| Wall-clock 105 min audio | ≥1500 s (timeout) | **180 s** | **passe sous le timeout Knative 300 s avec marge ×1.67** |
| RTF moyen 105 min | n/a (failed) | 0.029 | conforme au RTF d'autres services GPU |
| Utilisation GPU 105 min | 5 % moyen | 88 % sustained | bonne utilisation du capex L4 |
| `futex` syscalls / 3 s | 55 874 | 1 205 | thread thrashing éliminé |
| Qualité (nb segments / nb speakers) | 154 / 5 (13 min) | identique | **aucune régression** |

Le bug **#50 / round 5 #53** (échec du 105 min sur Kevent) devient résolu pour les usages métier Mirai jusqu'à environ 5h audio — bien au-delà de la cible 2h annoncée.

## Changements proposés

### 1. PR `hub-images` — branche `bench/diarization-perf`

Contenu de la branche :
- `diarization-api/diarizationserver/model.py` : ajout du flag `DIARIZATION_PRELOAD_WAVEFORM` (default `false`), implémentation `_load_waveform_tensor`, instrumentation hooks/JSONL, propagation `piste_tag`, exposition `clustering_min_cluster_size`.
- `diarization-api/diarizationserver/openai_routes.py` : ajout du paramètre form `piste_tag` (purement benchmark, optionnel, sans effet hors logging) et de `clustering_min_cluster_size`.

Action proposée à la merge :
- **Phase 1 (sans risque)** : merger en gardant `DIARIZATION_PRELOAD_WAVEFORM=false` par défaut. Permet le déploiement progressif. Activer le flag sur un seul replica en canary pendant 24 h, surveiller p50/p99 et taux d'erreur.
- **Phase 2 (changement de défaut)** : un commit séparé qui flippe le default à `true` une fois le canary validé.

### 2. PR `mirai-mesreunions` — branche `main`

Contenu :
- `docs/benchmarks/diarization/BASELINE_AUDIT.md`
- `docs/benchmarks/diarization/benchmark_runs.jsonl` (14 lignes)
- `docs/benchmarks/diarization/RESULTS.md`
- `docs/benchmarks/diarization/RECOMMENDATION.md`

Pas de changement de code applicatif. PR docs-only.

### 3. Côté `kevent-ai` (recommandé, hors scope de ce benchmark)

Aucune modification stricte requise. Mais une fois la piste 2 déployée sur le pod diarization :
- **Réviser à la baisse le `timeoutSeconds`** de l'`InferenceService` si actuellement à 300 s ou plus. Avec piste 2 active, un timeout à `240 s` suffit (marge ×1.33 sur le 180 s mesuré sur 105 min). Bloquera plus tôt les requêtes en queue anormalement longues.
- **Ne pas relâcher `containerConcurrency=2`** sans avoir d'abord wrapped `_diarize()` dans `run_in_threadpool` (hypothèse #2 du diagnostic initial, non corrigée). En l'état, deux requêtes concurrentes sur le même pod bloquent l'event loop FastAPI et les probes `/ready`. Suggestion : commit séparé (pas dans la PR piste 2) à `openai_routes.py` :
  ```python
  from starlette.concurrency import run_in_threadpool
  result = await run_in_threadpool(diarization_model._diarize, ...)
  ```

## Risques résiduels

| Risque | Probabilité | Impact | Mitigation |
|--------|-------------|--------|------------|
| Resample CPU bloquant en cas d'entrée non-16kHz | faible (Kevent uploade déjà en 16 kHz mono FLAC) | latence +1-2 s par requête | implémenter le resample côté GPU si besoin |
| OOM RAM sur audio extrême (>5 h) | très faible | crash pod | 5 h à 16 kHz mono float32 ≈ 1.1 GB, limite pod typiquement à 32 GB → marge ×29 |
| Pyannote met à jour son API et change `{"waveform", "sample_rate"}` | très faible | erreur 500 sur upgrade pyannote | test de non-régression à ajouter dans la CI |
| fp16 non implémenté manque un gain de 1.5-2× sur les phases GPU | acceptée | reste 100-150 s sur 105 min au lieu de potentiel 60-80 s | follow-up éventuel ; déjà bien au-delà du SLA actuel |

## Follow-up envisageables (non bloquants)

1. **Implémenter fp16 correctement** via `torch.autocast` autour de l'appel `self._pipeline(...)`. Gain attendu 1.5-2× sur la phase embeddings. À valider avec un audio annoté pour mesurer le DER (l'embedding ResNet34 wespeaker peut dériver de quelques milli-cosines en fp16).
2. **Wrapper sync `_diarize` dans `run_in_threadpool`** côté FastAPI pour libérer l'event loop et éviter le blocage des probes Knative (`/ready`, `/healthz`) pendant l'inférence. Indispensable si `containerConcurrency > 1`.
3. **Pousser le device transfer du waveform sur GPU** (`waveform.to("cuda")` avant l'appel pipeline). Économise un copy host→device par fenêtre. Gain estimé : 5-10 % sur la phase embeddings.
4. **Implémenter un endpoint `/v1/audio/diarizations:warmup`** qui charge un audio dummy au démarrage pour amortir le premier cold-start.

## Travail restant pour la mise en prod

- [ ] Review et merge des 2 PR (hub-images bench, mirai-mesreunions docs)
- [ ] Tag de l'image dans la CI (`diarization-api:v1.x.y-piste2`)
- [ ] Déploiement canary (1 replica avec flag activé) pendant 24 h
- [ ] Monitoring : p50/p99 latence, taux d'erreur, GPU util ; comparer à la baseline
- [ ] Si canary validé : commit séparé pour flipper le default à `true`, redéploiement complet
- [ ] Update du timeout Knative `kevent-diarization-predictor` à 240 s (séparé)
- [ ] Communication à l'équipe Kevent : Bug #50 résolu, fermeture #52 et #53 si applicable
