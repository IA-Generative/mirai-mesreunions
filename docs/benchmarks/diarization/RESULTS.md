# RESULTS — benchmark diarization-api

Mesures réalisées sur VM `openrag-et` (NVIDIA L4 24 GB, AMD EPYC 7413 8 cores) avec `pyannote/speaker-diarization-community-1`, container Docker `diarization-api:instr` (image instrumentée, branch `bench/diarization-perf`). Corpus : `short_13min.flac` (13 min, 154 segments, 5 speakers) et `long_105min.flac` (105 min, 1373 segments, 11 speakers), mono 16 kHz FLAC.

## Tableau récapitulatif

| piste | audio | wall (s) | RTF | phase dominante (s) | peak VRAM (MB) | nb speakers | nb segments | DER |
|-------|-------|----------|-----|---------------------|----------------|-------------|-------------|-----|
| **baseline** (3 runs) | 13 min | 115.7 (méd.) ±0.3 | 0.143 | embeddings 113.3 | 1629 | 5 | 154 | n/a |
| **baseline** (1 run, *abandonné à 1500 s*) | 105 min | **≥1500** (encore en embeddings au kill) | — | embeddings (CPU thrash, GPU 0 %) | 2254 | — | — | n/a |
| **piste 2 — preload waveform** (3 runs) | 13 min | **24.3** (méd.) ±2.1 | **0.030** | embeddings 23.3 | 1612 | 5 | 154 | n/a |
| **piste 2 — preload waveform** (3 runs) | 105 min | **180.3** (méd.) ±1.2 | **0.029** | embeddings 164.6 | 2254 | 11 | 1373 | n/a |
| piste 3 — fp16 partiel | 13 + 105 min | ❌ crash (HTTP 500) | — | — | — | — | — | — |
| pistes 1 / 4 / 5 / 6 | n/a | non testées | — | — | — | — | — | — |

DER non mesuré : pas d'annotation de référence disponible sur le corpus. Proxy qualité utilisé : identité bit-à-bit du nombre/longueur des segments entre runs et entre pistes.

## Profiling système — confirmation du diagnostic

Sur le 105 min, en pleine phase « GPU idle » (snapshot 3 secondes de `strace -c -p <pid>`) :

| syscall | baseline | piste 2 | facteur |
|---------|----------|---------|---------|
| `futex` | 55 874 (95.6 %) | 1 205 (1.4 %) | **/73** |
| `read` | 2 080 (4.2 %) | élargi (decode initial une fois) | — |
| `lseek` | 1 / 3 s | ~0 | — |

Stack py-spy en cours d'inférence :

| baseline | piste 2 |
|----------|---------|
| `get_embeddings → crop → torchcodec.decode` | `get_embeddings → forward (embedding ResNet)` |

Utilisation GPU sur 105 min :

| baseline | piste 2 |
|----------|---------|
| 100 % pendant ~4 min (segmentation), puis **0 %** sustained pendant le reste | **88 %** sustained pendant 3 min |

## Détail des speedups

| Audio | Baseline médiane | Piste 2 médiane | Speedup |
|-------|------------------|-----------------|---------|
| 13 min | 115.74 s | 24.29 s | **×4.76** |
| 105 min | ≥1500 s (n'a pas fini) | 180.30 s | **≥×8.32** (vrai gain probable **≥×13** d'après la pente CPU baseline) |

## Reproductibilité

Les 3 runs piste 2 sur 13 min sont stables à **±2.1 s** sur 24 s (~9 % d'écart). Les 3 runs piste 2 sur 105 min sont à **±1.2 s** sur 180 s (~0.7 % d'écart). Variance dominée par le scheduling kernel, pas par l'algorithme.

## Pistes non retenues (justification empirique)

### Piste 1 — `max_speakers` (gratuit, gain modeste)

Acte sur la coupe du dendrogramme dans le clustering. Or `discrete_diarization` (clustering inclus) = **2.4 s sur 179 s** soit 1.3 % du wall-clock total piste 2 sur 105 min. Gain max théorique 10-15 % × 2.4 s ≈ 0.4 s. **Non significatif sur ce pipeline.**

### Piste 3 — fp16 GPU

Implémentation simple `model.half()` essayée. Échec HTTP 500 sur les 4 runs avec :
```
RuntimeError: Expected tensor for argument #1 'input' to have the same type as
tensor for argument #2 'weight'; but type torch.cuda.FloatTensor does not
equal torch.cuda.HalfTensor (while checking arguments for cudnn_batch_norm)
```
Cause : le waveform préchargé est float32, le code applique `.half()` uniquement aux poids du `_segmentation` (l'attribut `model_` du `_embedding` n'est pas pris en compte par le pattern actuel `getattr(sub, "model")`). Une implémentation correcte nécessite soit `torch.autocast(device_type="cuda", dtype=torch.float16)` wrappant l'inférence, soit un cast input + dé-cast output. **Hors scope** — laissé comme follow-up envisageable dans `RECOMMENDATION.md`.

### Piste 4 — chunking + ré-association globale

Visait le mur O(N²) du clustering. Or sur ce pipeline le clustering reste à **2.4 s** sur le 105 min (avec piste 2). **Non applicable.**

### Piste 5 — tuning `min_duration_off` / `min_cluster_size` / `threshold`

Vise à réduire N en amont du clustering. Or le clustering ne domine pas. **Non significatif** sur ce pipeline. Conservé pour mémoire, à reconsidérer si on bascule vers un autre modèle où le clustering serait coûteux.

### Piste 6 — modèle alternatif

`pyannote/speaker-diarization-community-1` est déjà la version la plus récente et la plus rapide annoncée par pyannote. Pas de descendant plus performant disponible. **Non applicable** dans l'état actuel de l'écosystème.

## Configuration ayant produit les meilleurs résultats

```
DIARIZATION_PRELOAD_WAVEFORM=true
DIARIZATION_USE_FP16=false            # défaut (piste 3 non opérationnelle)
DIARIZATION_SEGMENTATION_BATCH_SIZE=32
DIARIZATION_EMBEDDING_BATCH_SIZE=32
```

Image : `diarization-api:instr` (= branche `bench/diarization-perf`, commit `9ece804`).
