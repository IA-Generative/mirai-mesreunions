# Karaoké YouTube — options de granularité timing transcript

> Compagnon de [`youtube-flow-sequence.md`](./youtube-flow-sequence.md).
> Étudie comment passer du timing brut YouTube (chunks 60-90s
> post-agrégation video-ingest) à une granularité utilisable pour la
> sync segments + navigation phrase ↔ player vidéo.

## 1. Problème

`youtube-transcript-api` retourne des chunks fins (~3-5s, mid-sentence),
mais `services/video_ingest/app/chunking.py` les agrège en chunks 60-90s
pour le RAG. Le résultat stocké dans `video_transcripts.segments_json`
a une granularité **insuffisante pour un karaoké au mot près** ou une
navigation phrase-par-phrase.

Comparaison de granularité (par référence Whisper+pyannote) :

| Niveau | Subtitle YouTube actuel | Whisper + pyannote |
|---|---|---|
| Timing | chunk 60-90s | mot (50-300ms) |
| Sentence boundaries | non | non (segments diarisation à la place) |
| Speakers | non | oui |

## 2. Trois solutions évidentes

### Solution A — Capture des fine-grained captions à l'ingestion

**Idée** : modifier `video-ingest/orchestrator.py` pour préserver les
captions ~3-5s issues directement de `youtube-transcript-api` en plus
des chunks 60-90s. Ajouter une colonne `fine_segments_json` dans
`video_transcripts`.

| Avantage | Inconvénient |
|---|---|
| Timing natif YouTube (le plus précis possible sans Whisper) | Migration BDD + double stockage |
| Pas d'interpolation/heuristique | Re-ingestion des 11 vidéos déjà en cache nécessaire |
| Faisable indépendamment du frontend | Granularité reste limitée par YouTube (~3-5s, pas mot-niveau) |

**Effort** : ~2h backend + 1 migration additive + script ré-ingestion.

### Solution B — Sentence-aligned + interpolation linéaire (RECOMMANDÉ)

**Idée** : à la matérialisation, redécouper les chunks 60-90s en
**phrases** via regex ponctuation (`. ! ? …`) et interpoler les
timecodes proportionnellement à la position en caractères.

**Test sur données réelles** (vidéos déjà ingérées en prod-bêta) :

```
Vidéo Mistral 89s :
  Avant : 2 chunks de 60-90s
  Après : 14 phrases, médiane 4.5s
  Exemples (start → end | texte) :
    0.04s  →  1.88s | Bah c'est quoi la philosophie open source ?
    1.88s → 10.05s | La philosophie open source, c'est que il y a une partie de ce qu'on construit en logiciel…
   10.05s → 23.15s | Et quand on est parti de Google et de Meta, l'intelligence artificielle qui est un peu…
   23.15s → 32.87s | Et donc quand on a lancé mistral, la première chose qu'on a fait…

Vidéo Mensch 4930s (1h22) :
  Avant : 82 chunks de 60-90s
  Après : 1038 phrases
  Durée par phrase : médiane 4.5s, p90 12s, max 37.8s
  Chars par phrase : médiane 85, max 656
```

| Avantage | Inconvénient |
|---|---|
| **Aucune migration BDD** : se calcule à la volée à la matérialisation | Précision ±2s sur une phrase de 10s (interpolation linéaire) |
| Format compatible Whisper (`[{start_seconds, end_seconds, text}]`) | Pas de speakers (mono-speaker fictif "Intervenant_01") |
| 1038 segments sur 1h22 = parfait pour nav phrase ↔ player | Phrases >20s sans ponctuation à mitiger (fallback `, ; :`) |
| **Réutilise les 11 vidéos en cache sans re-fetch** | Approximation des word-timings (déjà fait par `flatten_segments_to_synthetic_words`) |

**Effort** : ~2h backend dans `external_source.py` + 1h frontend pour
clic phrase → `player.seekTo(start_seconds)`. Aucune migration BDD.

**Mitigation phrases trop longues** : si une "phrase" excède 20s, la
recouper sur `,` ou `;` ; si toujours > 20s, sur les espaces tous les ~15s.

### Solution C — Force-asr Whisper (déjà disponible via Phase B)

**Idée** : pour les vidéos critiques (réunions parlementaires longues,
discours techniques), l'utilisateur coche `force_audio=true` à
l'import → le pipeline standard Whisper+pyannote produit transcript
avec **word-timings réels + speakers**. Livré tout à l'heure
(commit `39899b1`).

| Avantage | Inconvénient |
|---|---|
| Précision **mot** (~50-300ms) | Coût compute Kevent GPU |
| Speakers diarisés | Délai 5-15 min sur vidéo 1h |
| Format identique à un upload audio classique | Ne fonctionne pas si l'audio est purgé de S3-internal |

**Effort** : 0 (livré). UI gating en place (bouton "🔁 Transcription +
diarisation" visible pour `source_type='youtube_audio'`, masqué pour
`youtube_subtitle`).

## 3. Recommandation

**B en complément de C** :

- **B est livrable rapidement et couvre 100% des imports subtitle**
  (chemin par défaut). Aucun coût compute additionnel, pas de migration,
  re-exploite les vidéos déjà en cache.
- **C reste l'échappatoire** quand l'utilisateur a besoin de la
  qualité Whisper (vidéos techniques, locuteurs multiples à identifier).

UI prévue (frontend) :

```
[ Fiche détail YouTube ]
┌─────────────────────────────────────────────┐
│  ▶ IFrame player (embed YouTube)            │
│  📍 Curseur sync : phrase 47 / 1038         │
├─────────────────────────────────────────────┤
│  Liste phrases (cliquables → seekTo)        │
│  ⏵ 00:00 — Bah c'est quoi…                  │
│  ⏵ 00:02 — La philosophie open source…      │
│  ⏵ 00:10 — Et quand on est parti…           │← highlightée (player)
│  ⏵ 00:23 — Et donc quand on a lancé…        │
│  …                                          │
└─────────────────────────────────────────────┘
```

Le surlignage de la phrase courante se fait via polling
`player.getCurrentTime()` toutes les 150ms et binary search sur le
tableau de phrases (timing croissant). Pattern identique à celui
déjà en place pour les audios classiques.

## 4. Implémentation B — points clés

### Backend (`external_source.py`)

Nouvelle fonction `sentence_align_segments(coarse_segments)` :

```python
import re

_SENTENCE_RE = re.compile(r'(?<=[.!?…])\s+')
_LONG_PHRASE_THRESHOLD_SEC = 20.0

def sentence_align_segments(coarse_segments: list[dict]) -> list[dict]:
    """Re-segmente les chunks 60-90s en phrases sentence-aligned.

    Algorithme :
      1. Pour chaque chunk, split sur ponctuation finale (. ! ? …)
      2. Interpolation linéaire des timecodes via position en caractères
      3. Si phrase > 20s, fallback split sur ',' / ';'
      4. Dédup overlap entre chunks consécutifs (identité texte)
    """
    out: list[dict] = []
    for c in coarse_segments or []:
        text = (c.get("text") or "").strip()
        if not text: continue
        t0 = float(c["start_seconds"]); t1 = float(c["end_seconds"])
        dur = max(0.0, t1 - t0)
        if dur == 0: continue
        parts = _SENTENCE_RE.split(text)
        total = sum(len(p) for p in parts) or 1
        cursor = 0
        for p in parts:
            p = p.strip()
            if not p: continue
            sr = cursor / total; cursor += len(p) + 1; er = cursor / total
            seg = {"start_seconds": round(t0 + sr * dur, 2),
                   "end_seconds":   round(t0 + er * dur, 2), "text": p}
            # Recoupe si trop longue
            if seg["end_seconds"] - seg["start_seconds"] > _LONG_PHRASE_THRESHOLD_SEC:
                out.extend(_split_long_phrase(seg))
            else:
                out.append(seg)
    # Dédup overlap (les chunks 60-90s ont 15s d'overlap)
    return _dedupe_overlap(out)
```

À appeler dans le payload materialize :

```python
# external_source.py — materialize_payload_to_uaf_kwargs
sentence_segments = sentence_align_segments(payload["segments"])
words_json = flatten_segments_to_synthetic_words(sentence_segments)
uaf.transcription_words_json = json.dumps(words_json)
# segments_json (RAG-friendly) reste les 60-90s ; un nouveau champ
# transcription_segments_fine_json porte les phrases
```

### Frontend (`legacy.js` `mountTranscriptCorrector`)

Le rendu actuel sait déjà afficher des blocs speaker-tagged.
Adaptation pour YouTube :

```js
// data-source-type="youtube_subtitle" → utilise transcription_segments_fine_json
// data-source-type="upload" / "youtube_audio" → utilise speaker_tagged_text (existant)
const segments = isYoutubeSubtitle
    ? JSON.parse(audio.transcription_segments_fine_json || '[]')
    : parseSpeakerTagged(audio.speaker_tagged_text);

// Click handler : si player YT existant, seekTo
phraseEl.addEventListener('click', () => {
    if (ytPlayer) ytPlayer.seekTo(seg.start_seconds, true);
});

// Polling 150ms pour highlight phrase courante
setInterval(() => {
    if (!ytPlayer) return;
    const t = ytPlayer.getCurrentTime();
    const idx = binarySearch(segments, t);
    highlightPhrase(idx);
}, 150);
```

### Migration BDD

Aucune. Le champ `transcription_words_json` est déjà rempli pour
YouTube (synthétique). On change juste la SOURCE de calcul des mots :
au lieu de partir des 60-90s, on part des phrases sentence-aligned.

Un nouveau champ optionnel `transcription_segments_fine_json` pourrait
être ajouté pour stocker les phrases en clair (sinon le frontend
re-segmente côté JS). À trancher selon le coût bandwidth.

## 5. Plan d'exécution suggéré

| Slice | Description | Effort |
|---|---|---|
| K1 | `sentence_align_segments` + tests unitaires | 1h |
| K2 | Intégration dans `materialize` (subtitle path) | 30 min |
| K3 | Frontend : binary search + highlight + seekTo | 2h |
| K4 | Backfill : re-matérialiser les 11 vidéos en cache | 30 min |
| K5 | Mesure UX : feedback user sur précision ±2s | continu |

Total ≈ 4h. Aucun risque sur le chemin audio classique
(`source_type='upload'`) qui reste sur speaker_tagged_text legacy.
