# Diarization backend — selector & runtime

Cet article décrit le toggle `DIARIZATION_BACKEND` qui permet à
`internal-ingester` (service `dmz-to-internal-bridge`) de choisir entre
deux backends pour la diarisation, **sans toucher au backend de
transcription** (qui reste sur le gateway Kevent).

## Pourquoi deux backends

La diarisation pyannote sur long audio est sensible au timeout HTTP du
gateway Mirai (cf kevent-ai#53 sur les anomalies RTF MIG10/MIG20). Quand
l'image gateway n'a pas la build qui pré-charge la waveform
(`DIARIZATION_PRELOAD_WAVEFORM=true` côté `diarization-api` ≥ v2.5-gpu),
on observe :

- `diarize` (sync, POST `/v1/audio/diarizations`) : ~400-500 s d'attente
  côté caller, le LB SCW coupe vers 300 s sur certains profils.
- `diarize_async` (job-based) : OK fonctionnellement mais TTL court côté
  gateway (issue #49) + occupation de slot.

Quand un container `diarization-api` v2.5-gpu est joignable directement
(par exemple sur la VM `build-vm` <vm-diarization-host>:8080), il traite
105 min d'audio en ~3 min. Le backend `vm-direct` court-circuite alors
le gateway et tape ce container en sync.

## Backends supportés

| `DIARIZATION_BACKEND` | Route diarisation | Notes |
|---|---|---|
| `kevent` (défaut) | gateway Mirai | sync ou async selon `KEVENT_ASYNC_MODE`. Comportement historique. |
| `vm-direct` | container `diarization-api` à `DIARIZATION_VM_URL` | sync. Requiert que l'image VM ait `DIARIZATION_PRELOAD_WAVEFORM=true`. Auth = `KEVENT_API_KEY` réutilisée comme `Authorization: Bearer` (l'nginx-gate devant la VM accepte le token kevent). |

Le toggle n'affecte **que** la diarisation. La transcription Whisper et
tous les autres steps (LLM, glossaire, méta) restent sur le gateway
Kevent quel que soit le backend choisi.

## Variables d'environnement

| Variable | Type | Défaut | Rôle |
|---|---|---|---|
| `DIARIZATION_BACKEND` | string | `kevent` | `kevent` ou `vm-direct`. Toute autre valeur lève `ValueError` au démarrage du puller. |
| `DIARIZATION_VM_URL` | string | `""` | URL racine du container VM (sans `/v1/audio/diarizations`). Obligatoire quand `DIARIZATION_BACKEND=vm-direct`. |

## Exemple de manifeste (prod-bêta interne)

```yaml
- name: DIARIZATION_BACKEND
  value: "vm-direct"
- name: DIARIZATION_VM_URL
  value: "http://<vm-diarization-host>:8080"
```

Pour revenir au gateway Kevent : retirer `DIARIZATION_VM_URL` (ou laisser
vide) et passer `DIARIZATION_BACKEND` à `kevent` (ou retirer la variable
— c'est le défaut). Un `kubectl rollout restart deployment/internal-ingester`
suffit, pas de rebuild image.

## Détails d'implémentation

- Code : [`services/dmz-to-internal-bridge/app/kevent_client.py`](../services/dmz-to-internal-bridge/app/kevent_client.py)
  — méthodes `diarize` et `diarize_async` dispatchent vers `_diarize_vm`
  quand `self.diarization_backend == "vm-direct"`.
- Build du client : [`services/dmz-to-internal-bridge/app/puller.py`](../services/dmz-to-internal-bridge/app/puller.py)
  `_build_kevent_client()`.
- Le helper `_diarize_vm` utilise `urllib.request` (stdlib) et non
  `requests==2.32.3`. Vérifié empiriquement 2026-05-16 : derrière le
  `diarize-gate` nginx devant la VM, `requests` envoie un multipart que
  nginx rejette en HTTP 400 alors que `curl`, `httpx` et `urllib` passent
  avec le même payload + même header. On évite d'ajouter `httpx` comme
  dépendance juste pour ça.
- Les erreurs sont classées dans les mêmes familles que les appels
  gateway (`KeventAuthError`, `KeventTransientError`, `KeventApplicativeError`),
  pour que le caller (queue de retry du puller) réagisse pareil dans les
  deux modes.

## Bench

Voir `docs/benchmarks/diarization/` pour les RTF baselines sur les trois
plateformes (VM L4 direct, MIG10 prod, MIG20 prod).
