# BASELINE_AUDIT — diarization-api

Audit du code existant avant toute optimisation. Références : `hub-images` worktree `bench/diarization-perf`, dossier `diarization-api/diarizationserver/`.

## 1. Pipeline pyannote utilisé

- **Modèle** : `pyannote/speaker-diarization-community-1`
- Source : `diarizationserver/model.py:23` (default constructor) et `diarizationserver/__main__.py:35` (default CLI `--model_id`).
- Chargement effectif : `model.py:82` — `Pipeline.from_pretrained(model_path, token=hf_token)`.
- Conséquence : **clustering = AgglomerativeClustering sklearn**, pas VBx. Les paramètres `clustering_Fa` / `clustering_Fb` exposés dans l'API (`openai_routes.py:39-41`, `model.py:117-119`) sont **morts** sur ce pipeline. Seuls `clustering.threshold`, `clustering.min_cluster_size` et `max_speakers` ont un effet.

## 2. Cycle de vie du pipeline

- Chargement **une seule fois** au démarrage via lifespan FastAPI (`__main__.py:90-119`, méthode `DiarizationModel.load()`).
- Pas de rechargement par requête. ✓
- Mais `_diarize` appelle `_instantiate_pipeline(...)` (`model.py:152-153`) qui exécute `pipeline.instantiate(config)` si les hyperparamètres diffèrent du précédent appel — c'est un re-bind de la config interne, pas un re-chargement de poids. Coût acceptable, mais expose une légère contention si requêtes concurrentes alternent les params (hors scope ici, mono-worker).

## 3. Passage de l'audio au pipeline

- Path actuel : **fichier sur disque**, chemin string passé à `pipeline(audio_path, ...)` (`model.py:169`).
- En entrée :
  - via API multipart `/v1/audio/diarizations` → `tempfile.NamedTemporaryFile` sur disque (`openai_routes.py:46-49`), suffixe = extension du fichier client.
  - via `predict` JSON (`predict`/KServe) → `audio_content` base64 → fichier temp disque (`model.py:200-205`), ou `audio_path` direct.
- Aucun pré-chargement tensor, aucune conversion explicite vers 16 kHz mono côté serveur.
- **Conséquence directe** : pyannote re-décode via `torchcodec` à chaque fenêtre d'embedding (cf. diagnostic doc benchmark — stack `get_embeddings → crop → torchcodec`, 95.6 % futex). C'est le bottleneck.
- Sample rate / canaux d'entrée : non contrôlés. Les FLAC du corpus de test sont déjà à 16 kHz mono donc le resample interne pyannote est neutre — seul le re-décodage par fenêtre coûte.

## 4. Hyperparamètres fixés

Defaults (depuis env / CLI) :

| paramètre | valeur | source |
|-----------|--------|--------|
| `min_speakers` | 1 | `model.py:26`, env `DIARIZATION_MIN_SPEAKERS=1` |
| `max_speakers` | 10 | `model.py:27`, env `DIARIZATION_MAX_SPEAKERS=10` |
| `segmentation_batch_size` | 32 | `model.py:92`, env `DIARIZATION_SEGMENTATION_BATCH_SIZE` |
| `embedding_batch_size` | 32 | `model.py:93`, env `DIARIZATION_EMBEDDING_BATCH_SIZE` |

Hyperparams propagés au runtime via payload requête :
- `min_duration_off` → `pipeline.instantiate({"segmentation": {"min_duration_off": ...}})` (`model.py:108-110`).
- `clustering_threshold` → `pipeline.instantiate({"clustering": {"threshold": ...}})` (`model.py:114-115`).
- `clustering_Fa` / `clustering_Fb` → exposés mais **inopérants** sur community-1 (cf. §1).
- `num_speakers` / `min_speakers` / `max_speakers` → passés à l'appel pipeline (`model.py:155-167`).

À noter : `min_cluster_size` n'est pas exposé. Piste 5 demandera de l'ajouter.

## 5. Configuration GPU

- Sélection device : `model.py:39-43` — CUDA si dispo et `use_gpu=true` (default), sinon CPU.
- Move pipeline sur GPU : `model.py:88-89` — `self._pipeline = self._pipeline.to(torch.device("cuda"))`.
- Précision : **fp32**. Aucun cast `.half()`, aucun autocast. Piste 3 nécessitera d'ajouter un flag.
- Aucune gestion explicite de mémoire (pas de `empty_cache`, pas de stream custom). VRAM observée à load : 2.2 GB sur L4 24GB → marge confortable.

## 6. Points notables hors audit demandé

- `_diarize` mesure `processing_time` côté API (`model.py:140, 183`) mais ne décompose pas par phase pyannote. **Instrumentation à ajouter** : `ProgressHook` pour timings segmentation / embedding / clustering, `torch.cuda.max_memory_allocated` pour peak VRAM, log JSONL append-only.
- Aucun monkey-patch GPU/CUDA suspect. Le seul patch est `torch.load` forcé `weights_only=False` (model.py:74-85), nécessaire pour pyannote 4.x.
- Le contrat API publique (input/output JSON) n'a pas à changer pour les pistes 1-3-5-6. Pour piste 2 le contrat reste identique (le préchargement est interne au serveur).

## Conclusion

Diagnostic du doc benchmark **confirmé par lecture du code** : aucun pré-chargement tensor → re-décodage torchcodec par fenêtre → futex thrashing. La piste 2 (preload waveform) est implémentable en quelques lignes dans `_diarize` ou en amont (préprocess). Le contrat ne bouge pas. Tous les autres leviers (1, 3, 5, 6) sont du tuning de paramètres déjà partiellement exposés. La piste 4 (chunking) demanderait un module à part.
