# Benchmark des optimisations de diarisation — mcr-secure-audio

## Objectif

Réduire le wall-clock de la diarisation `pyannote` sur audios longs (>60 min) **sans dégrader la qualité de regroupement des locuteurs**.

On observe sur un audio de 105 min un profil GPU dent-de-scie : pic à 100 % pendant les toutes premières minutes, puis 0 % prolongé pendant que le CPU à ~224 % (≈2 cores) continue de tourner. Sur un 13 min court, le GPU reste utilisé tout du long.

## Diagnostic confirmé (à jour)

Profil syscalls observé pendant la phase « GPU idle » :

| syscall | % temps | calls (3s) |
|---------|---------|-----------|
| futex   | **95.6 %** | 55 874 |
| read    | 4.2 %  | 2080 |
| autres  | 0.2 %  | ~75 |

Combiné à un `py-spy` qui montre la stack `get_embeddings → crop → torchcodec`, le verdict est sans ambiguïté :

**Le bottleneck N'EST PAS le clustering CPU. C'est l'extraction d'embeddings qui starve le GPU à cause d'un I/O audio par fenêtre.**

Pyannote itère sur des milliers de fenêtres → pour chaque batch, `crop` re-décode l'audio via `torchcodec` depuis le fichier. Les threads (worker pool torchcodec + GIL Python + torch CPU) se synchronisent en boucle → 56k futex en 3 s. Le GPU attend que ces threads lui livrent un batch préparé. Augmenter `embedding_batch_size` ne change rien tant qu'on est dans ce régime : le goulot n'est pas la taille du batch, c'est le débit de production CPU.

**Implication directe** : la piste 2 (audio en mémoire, tensor pré-chargé) n'est plus une optimisation marginale, c'est **le fix principal** — promue priorité #1. Les autres pistes restent à benchmarker mais après celui-ci, et **sur une baseline post-fix**, parce que le profil du wall-clock va probablement complètement changer (le GPU va saturer la segmentation+embedding → la phase clustering pourrait redevenir la dominante).

Ta mission :
1. **Mesurer** la baseline actuelle proprement (instrumentée).
2. **Appliquer le fix d'I/O (piste 2)** en priorité.
3. **Remesurer** la nouvelle baseline.
4. **Tester les autres pistes une par une** sur cette nouvelle baseline.
5. **Recommander** la ou les optimisations à garder.

---

## Étape 0 — Cadrage du code existant

Avant toute optimisation, fais une cartographie du pipeline actuel et réponds par écrit aux questions suivantes :

1. **Quel pipeline pyannote est utilisé ?** Cherche les appels `Pipeline.from_pretrained(...)`. Note précisément le nom de modèle et la version (ex : `pyannote/speaker-diarization-3.1`, `pyannote/speaker-diarization-community-1`, un pipeline VBx custom, etc.).
2. **Le pipeline est-il chargé une seule fois** au démarrage du process (worker, FastAPI startup event) ou rechargé à chaque requête ? Si c'est le second cas, c'est un bug à corriger avant de benchmarker quoi que ce soit.
3. **Comment l'audio est-il passé au pipeline ?** Fichier temporaire sur disque ? `BytesIO` ? Tensor en mémoire ? À quel sample rate / nombre de canaux arrive l'audio en entrée du pipeline ?
4. **Quels hyperparamètres sont fixés ?** `clustering.threshold`, `clustering.min_cluster_size`, `segmentation.min_duration_off`, `min_speakers`, `max_speakers`, params VBx `Fa`/`Fb` si applicable.
5. **Quelle est la configuration GPU ?** Précision (`fp32`/`fp16`), device explicite, gestion mémoire.

Produis un petit fichier `BASELINE_AUDIT.md` avec ces réponses, en citant les chemins de fichiers et numéros de ligne. C'est la référence pour tout le reste.

> ⚠️ Point important : les paramètres `clustering_Fa` et `clustering_Fb` n'existent que sur les pipelines **VBx-HMM** (typiquement `pyannote/speaker-diarization` 2.x ou un pipeline Brno custom). Sur `speaker-diarization-3.1` et `community-1`, le clustering est un `AgglomerativeClustering` sklearn ; les leviers sont alors `clustering.threshold`, `clustering.min_cluster_size`, et `max_speakers`. Adapte tes pistes en conséquence.

---

## Étape 1 — Instrumentation et baseline

**Ne touche pas aux hyperparamètres tant que tu n'as pas une baseline mesurée.**

### Instrumentation à ajouter autour de l'appel au pipeline

- `time.perf_counter()` total autour de `pipeline(...)`.
- Décomposition par phase via le `ProgressHook` de pyannote (`from pyannote.audio.pipelines.utils.hook import ProgressHook`). Capture les timings de `segmentation`, `embedding`, `clustering`, et toute autre phase exposée par le hook.
- `torch.cuda.max_memory_allocated()` avant/après pour la VRAM peak.
- Optionnel mais utile : log le nombre de fenêtres d'embedding produites (taille de la matrice de distances clusterisée).

### Profiling système (à faire au moins une fois sur un long audio)

Le diagnostic actuel a été établi grâce à ce profiling — c'est ce qui permet de distinguer un goulot d'I/O (futex thrashing) d'un vrai goulot CPU compute (clustering). À refaire après chaque modif structurelle :

- **`strace -c -p <pid>`** pendant 3–5 s en pleine phase « GPU idle » → si `futex` > 80 % du temps, c'est de la contention de threads (typiquement I/O audio bloquant le pipeline). Si c'est plutôt du `read` ou du compute pur, c'est autre chose.
- **`py-spy dump --pid <pid>`** ou **`py-spy record`** sur 10–20 s → identifie la stack Python active. Cherche notamment `get_embeddings`, `crop`, `torchcodec`, `linkage`, `pdist` pour situer la phase.
- **`nvidia-smi dmon -s u`** en parallèle → utilisation GPU instantanée. Corrobore avec le timing par phase.

Si après le fix d'I/O (piste 2) tu vois encore du GPU idle prolongé : refaire ce profiling — la nature du goulot aura changé.

Format de log : un objet JSON par run, écrit dans un fichier `benchmark_runs.jsonl` (append-only). Champs minimum :
```
{
  "run_id": "...",
  "audio_path": "...",
  "audio_duration_s": float,
  "piste": "baseline" | "piste_1_max_speakers" | ...,
  "config": { ... },        # paramètres effectifs utilisés
  "wall_clock_s": float,
  "rtf": float,             # wall_clock / audio_duration
  "phase_times_s": { "segmentation": ..., "embedding": ..., "clustering": ... },
  "peak_vram_mb": float,
  "nb_speakers_detected": int,
  "nb_segments_output": int,
  "der": float | null       # si annotation de référence disponible
}
```

### Corpus de test

Constitue un corpus d'au moins **3 audios** de durées contrastées :
- court (~13 min)
- moyen (~45 min)
- long (~105 min)

Si possible : ajouter un audio avec annotation de référence pour mesurer le `DiarizationErrorRate` (`pyannote.metrics`).

À défaut de référence annotée, utilise comme **proxy qualité** :
- nombre de locuteurs détectés
- longueur médiane et p10 des segments
- comparaison visuelle d'un extrait sur 5 min entre baseline et variante (export en RTTM ou simple JSON puis diff)

### Baseline

Lance **3 runs** par audio avec la configuration actuelle, sans aucune modification d'hyperparamètre. Calcule médiane + écart pour chaque métrique. C'est ta référence.

---

## Étape 2 — Pistes à tester

**Règle** : une piste à la fois, mêmes audios que la baseline, 3 runs chacune. Ne combine pas avant d'avoir mesuré individuellement. Toutes les pistes doivent être activables/désactivables via une variable d'env ou un flag — ne change pas les defaults tant qu'une piste n'est pas validée.

### Piste 1 — Borner `max_speakers` (gratuit, gain modeste)

Si l'appel actuel est `pipeline(audio)`, passe `max_speakers` :
```python
pipeline(audio, max_speakers=10)
```
Tester les valeurs `6`, `10`, `15`. Sur des réunions, 10 est généralement très large.

Encore mieux si le contexte applicatif fournit le nombre de participants attendus : passer `num_speakers=N` directement, ou un couple `(min_speakers, max_speakers)` resserré.

**Gain attendu : 5–15 % sur le wall-clock total**, pas plus. Important à comprendre : dans `pyannote/speaker-diarization-3.1`, le linkage (`scipy.linkage`) est calculé sur **tous les N embeddings indépendamment de `max_speakers`**. Le paramètre n'agit que sur la coupe du dendrogramme (rapide) et le post-traitement. Ce n'est donc **pas** la réponse au problème des audios longs. On le teste parce que c'est gratuit et sans risque qualité (tant que `max_speakers` ≥ nombre réel de locuteurs).

Le vrai levier sur la phase clustering, c'est de **réduire N** lui-même → voir pistes 4 et 5.

### Piste 2 — Audio en mémoire, tensor pré-chargé (PRIORITÉ #1, fix du goulot identifié)

C'est **le fix principal** d'après le diagnostic syscall + py-spy. Sans ce changement, pyannote re-décode l'audio depuis le fichier à chaque fenêtre d'embedding via torchcodec → 95 % du temps en futex (synchro de threads) → GPU starve.

Avec un waveform tensor en mémoire, `crop` devient un simple slicing tensor (microseconde) au lieu d'un `decode + seek` torchcodec. Pour 105 min FLAC 16 kHz mono : ~200 MB en RAM (float32), trivial.

```python
import torchaudio
waveform, sr = torchaudio.load(source)  # ou torchcodec, une seule fois
if sr != 16000:
    waveform = torchaudio.functional.resample(waveform, sr, 16000)
    sr = 16000
if waveform.shape[0] > 1:
    waveform = waveform.mean(dim=0, keepdim=True)
pipeline({"waveform": waveform, "sample_rate": sr})
```

Pyannote travaille nativement à 16 kHz mono. Tout sample rate plus haut implique un resample interne en CPU pendant la phase segmentation/embedding. Pour les audios déjà à 16 kHz mono, le bénéfice du resample est nul mais l'élimination du re-decode par fenêtre reste massive.

**Gain attendu : 5–10× sur la phase embedding** (basé sur le profil futex à 95 %). Le GPU devrait passer de 0 % à proche de 100 % en continu pendant toute cette phase.

**Vérifications obligatoires** :
- la sortie reste équivalente à la baseline (timestamps à ±50 ms près, mêmes labels modulo permutation)
- refaire le profiling syscall + py-spy après le fix → confirmer que `futex %` chute drastiquement
- mesurer le wall-clock par phase : la phase clustering pourrait redevenir la dominante, ce qui réactive l'intérêt des pistes 4 et 5

**Une fois ce fix appliqué et validé, il devient la nouvelle baseline pour tester les autres pistes.**

### Piste 3 — Inférence fp16 sur GPU

Convertir les modèles internes en half precision :
```python
pipeline.to(torch.device("cuda"))
for attr in ("_segmentation", "_embedding"):
    sub = getattr(pipeline, attr, None)
    if sub is not None and hasattr(sub, "model"):
        sub.model.half()
```
Les noms d'attributs exacts dépendent de la version de pyannote — adapte en inspectant `pipeline.__dict__`.

Gain attendu sur **segmentation** et **embedding** (1.5–2×). À ne tester **que si** ces phases dominent le wall-clock sur ton corpus. Vérifier DER inchangé.

### Piste 4 — Chunking + ré-association globale (LE vrai levier sur très long audio)

Le plus de code à écrire, mais c'est la seule réponse structurelle au mur `O(N²)` du clustering.

**Pourquoi ça marche, calcul rapide** : sur 105 min avec stride pyannote par défaut, on a ~7000 embeddings → `linkage` coût ~`7000² ≈ 49M` opérations. Découpé en 4 blocs de ~30 min : 4 × `1750² ≈ 12M`, plus un mini-linkage sur les ~40 centroïdes (négligeable). **Facteur ~4× sur la phase dominante**, et le GPU reste saturé d'un bloc à l'autre (au lieu d'être à 0 % pendant tout le clustering global).

**Algorithme** :
1. Découper l'audio en blocs de **30 min** avec **30 s de chevauchement** entre blocs consécutifs.
2. Diariser chaque bloc indépendamment (séquentiel, le GPU est réutilisé d'un bloc à l'autre — il reste à 100 % en continu).
3. Pour chaque bloc, récupérer un **embedding centroïde par locuteur** détecté dans ce bloc. Soit via la sortie `return_embeddings=True` de pyannote si disponible, soit en ré-extrayant via le modèle d'embedding sur les segments du locuteur.
4. Re-clusteriser les centroïdes inter-blocs (ils sont peu nombreux : `nb_blocs × nb_speakers_par_bloc`) avec un `AgglomerativeClustering` configuré avec le même `threshold` que pyannote en interne.
5. Réassigner les labels globaux en utilisant la zone de chevauchement comme contrainte : un locuteur actif aux 30 dernières secondes du bloc N et aux 30 premières du bloc N+1 doit recevoir le même label global.

Implémente ça dans un module séparé (par exemple `chunked_diarization.py`) et active-le via flag.

**Tests qualité obligatoires** : sur un audio annoté, comparer le DER chunked vs baseline. Tolérance : +1 point absolu max.

### Piste 5 — Tuner `min_duration_off`, `min_cluster_size`, `threshold` (levier sous-estimé)

Petit grid search :
- `min_duration_off ∈ {1.5, 2.0, 2.5, 3.0}` — silence minimum pour fragmenter
- `min_cluster_size ∈ {12, 20, 30}` — taille minimum d'un cluster
- `threshold` actuel ± 0.05

**Pourquoi `min_duration_off` est plus puissant qu'il n'en a l'air** : il agit **en amont** sur la segmentation. Moins de fragments parlés → moins de fenêtres embedées → **N plus petit** → coût `O(N²)` du linkage qui chute **quadratiquement**. Passer de 1.5s à 2.5s peut diviser N par ~1.3–1.5 → wall-clock clustering divisé par 1.7–2.2.

Trade-off : on rate les changements de tour très rapides (interruptions, dialogues vifs). À benchmarker DER en priorité ici.

### Piste 6 — Modèle alternatif

Si la baseline tourne sur `pyannote/speaker-diarization-3.1`, tester `pyannote/speaker-diarization-community-1` (annoncé 3–3.5× plus rapide à qualité équivalente, drop-in). Mesurer DER et wall-clock comparés à 3.1.

Si la baseline est un pipeline VBx ancien, tester aussi le passage à 3.1 ou community-1 comme alternative plus rapide (et c'est une mise à jour de toute façon souhaitable).

---

## Étape 3 — Combinaison et recommandation finale

Une fois les pistes mesurées individuellement :
1. Combiner les 2 meilleures (typiquement piste 1 + piste 4 si clustering dominait, ou piste 2 + piste 3 si segmentation/embedding dominait) et remesurer sur le corpus complet.
2. Vérifier qu'il n'y a pas de régression qualité (DER, nombre de locuteurs, segments aberrants).

---

## Livrables attendus

Dans le dossier `docs/benchmarks/diarization/` :

1. **`BASELINE_AUDIT.md`** — produit à l'étape 0.
2. **`benchmark_runs.jsonl`** — toutes les exécutions, append-only.
3. **`RESULTS.md`** — un tableau récap pour chaque piste :

   | piste | audio | wall_clock (s) | RTF | phase dominante (s) | peak VRAM | nb_speakers | DER |
   |-------|-------|----------------|-----|---------------------|-----------|-------------|-----|

4. **`RECOMMENDATION.md`** — une page :
   - quelles 1 à 3 pistes garder (potentiellement combinées) et pourquoi
   - changements de defaults proposés (ou flags à exposer)
   - risques résiduels et cas dégradés observés
   - travail restant pour mise en prod

---

## Contraintes

- Toute nouvelle option doit être **derrière un flag** (variable d'env, paramètre de config) avec **valeur par défaut = comportement actuel**. Ne change pas les defaults tant qu'une piste n'est pas validée.
- Pas de combinaison de pistes avant d'avoir mesuré chacune individuellement.
- Aucune modification du contrat d'API publique (input/output shape) tant que les benchmarks ne sont pas conclus.
- Si une piste fait baisser le wall-clock mais augmente le DER de plus de +1 point absolu, elle est rejetée — l'objectif est la latence **sans** dégradation de qualité.

---

## Démarrage suggéré

1. Étape 0 (audit) → produit `BASELINE_AUDIT.md`.
2. Étape 1 (instrumentation + baseline avec profiling syscall + py-spy) → produit les premières lignes de `benchmark_runs.jsonl` **et** confirme (ou infirme) le diagnostic « futex thrashing dans get_embeddings → crop → torchcodec ».
3. **Appliquer piste 2 (waveform en mémoire)** — c'est le fix prioritaire identifié. Remesurer entièrement, refaire le profiling syscall pour valider que `futex %` a chuté. C'est la nouvelle baseline.
4. À partir de cette nouvelle baseline, regarder la phase qui domine désormais le wall-clock :
   - **Clustering est devenu dominant** → tester dans cet ordre : **5** (gros levier sur N, peu de code), **1** (gratuit, gain modeste), **4** (gros levier structurel, plus de code).
   - **Segmentation/embedding dominent encore** mais avec GPU saturé → tester **3** (fp16) et **6** (modèle community-1).
5. Combiner les 2 meilleures, remesurer, écrire `RECOMMENDATION.md`.

**Ordre de gain attendu** (du plus rentable au moins) :

| piste | gain attendu | effort | risque qualité | quand |
|-------|--------------|--------|----------------|-------|
| **2 (waveform tensor en mémoire)** | **5–10× phase embedding** | faible | très faible | **EN PRIORITÉ — fix du goulot identifié** |
| 5 (tuning `min_duration_off`) | 1.5–2× sur clustering | faible | moyen (DER à surveiller) | après piste 2, si clustering domine |
| 4 (chunking + re-clustering) | 3–4× sur clustering, GPU saturé | élevé | moyen-élevé | après piste 2, si clustering domine encore |
| 6 (modèle community-1) | jusqu'à 3× global si applicable | faible | à valider | après piste 2 |
| 3 (fp16 GPU) | 1.5–2× sur segmentation+embedding | faible | faible | après piste 2 |
| 1 (`max_speakers`) | 5–15 % wall-clock total | nul | nul | après piste 2, en complément |

Stoppe-toi et demande validation avant d'implémenter la piste 4 (chunking) — c'est la plus grosse modif et la plus risquée pour la qualité. Si la piste 2 suffit à ramener le RTF à un niveau acceptable, la piste 4 peut être abandonnée.
