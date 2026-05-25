# ADR 0002 — Enum statut pipeline comme source de vérité unique, vérifié au build

| | |
|---|---|
| **Statut** | Proposé |
| **Date** | 2026-05-25 |
| **Auteurs** | Eric Tiquet + Claude Code |
| **Décideurs** | Eric Tiquet (PO/tech lead) |
| **Successeur de** | (aucun) |
| **Lié à** | [ADR-0001 — Pipeline liveness vs progress](0001-pipeline-liveness-vs-progress.md), [chantier résilience](../chantier-resilience-batch-processing.md) |
| **Périmètre** | Pattern de codage applicable à `transcription_status` (UserAudioFile) et tout futur état de pipeline |

## 1. Contexte (à lire avant tout)

L'application gère un pipeline asynchrone (upload audio → transcoding
→ Whisper → diarization → LLM chain → CR). Chaque étape pose un
statut dans `user_audio_files.transcription_status`. Le pipeline est
distribué entre 2 clusters Kubernetes (zone externe DMZ + zone
interne), avec un watchdog dédié pour récupérer les jobs orphelins.

ADR-0001 a posé le principe « séparer liveness du worker et progress
du traitement » et livré 4 fixs majeurs (heartbeat Kevent, flock
multi-worker, MCR resume, observabilité erreur).

Cet ADR pose la **convention de codage** qui DOIT garantir qu'on
n'introduise plus jamais de statut "orphelin" — un statut posé en
code qui n'est dans aucune liste de surveillance, et qui peut donc
laisser une row en purgatoire éternel.

## 2. Le problème en termes simples

Aujourd'hui :
- Les statuts possibles sont des **strings littérales** dispersées
  dans le code (`"kevent_failed"`, `"mcr_pushed"`, etc.)
- Le watchdog maintient 2 sets (`NON_TERMINAL_STATUSES`,
  `RETRYABLE_TERMINAL_STATUSES`) qui DEVRAIENT couvrir tous les
  statuts intermédiaires/d'échec
- Rien dans le code ne **vérifie** que les statuts posés sont dans
  l'un ou l'autre set, ou catégorisés explicitement comme terminal-OK

Conséquence observée : à chaque nouveau statut introduit (`kevent_reprocessing`
en avril, `s3_object_purged` en mai, `mcr_unavailable_on_source` en
mai), la même règle d'or est oubliée et l'incident survient.

**5 incidents en 4 semaines, tous de la même classe.** L'humain
oublie, la base de code ne le rappelle pas, la régression revient.

## 3. Décision

Adopter un **enum centralisé + 4 sets exhaustifs + check au build** :

### 3.1. Source de vérité unique

Nouveau fichier `libs/shared/app/pipeline_states.py` :

```python
from enum import StrEnum

class PipelineStatus(StrEnum):
    """Énumère TOUS les statuts possibles de transcription_status.

    Toute valeur littérale assignée à user_audio_files.transcription_status
    dans le code DOIT correspondre à un membre de cet enum. Un check CI
    fait échouer le build si une string non-enum est trouvée.
    """
    # Phase 1 — Avant Kevent
    PENDING = "pending"
    TRANSFERRING = "transferring"
    TRANSCODING = "transcoding"
    SCANNING = "scanning"

    # Phase 2 — Kevent in-flight
    KEVENT_QUEUED = "kevent_queued"
    KEVENT_PROCESSING = "kevent_processing"
    KEVENT_TRANSCRIBING = "kevent_transcribing"
    KEVENT_REPROCESSING = "kevent_reprocessing"

    # Phase 3 — Terminaux succès
    KEVENT_COMPLETED = "kevent_completed"
    KEVENT_PARTIALLY_COMPLETED = "kevent_partially_completed"
    COMPLETED = "completed"  # legacy
    MCR_PUSHED = "mcr_pushed"
    MCR_TRANSCRIPT_ONLY = "mcr_transcript_only"

    # Phase 3bis — Terminaux échec retryable (bouton "Relancer")
    KEVENT_FAILED = "kevent_failed"
    MCR_IMPORT_FAILED = "mcr_import_failed"
    MCR_PUSH_FAILED = "mcr_push_failed"
    FAILED = "failed"  # legacy

    # Phase 3ter — Terminaux échec non-retryable (action user requise)
    MCR_AUTH_FAILED = "mcr_auth_failed"
    MCR_REJECTED = "mcr_rejected"
    MCR_UNAVAILABLE_ON_SOURCE = "mcr_unavailable_on_source"
    QUARANTINED = "quarantined"
    TRANSCODE_FAILED = "transcode_failed"
    DISABLED = "disabled"

    # Import MCR
    MCR_IMPORT_PENDING = "mcr_import_pending"

    # ... etc (liste exhaustive)


# Sets de catégorisation. SOURCE DE VÉRITÉ pour le watchdog.
NON_TERMINAL: frozenset[PipelineStatus] = frozenset({
    PipelineStatus.PENDING,
    PipelineStatus.TRANSFERRING,
    PipelineStatus.TRANSCODING,
    PipelineStatus.SCANNING,
    PipelineStatus.KEVENT_QUEUED,
    PipelineStatus.KEVENT_PROCESSING,
    PipelineStatus.KEVENT_TRANSCRIBING,
    PipelineStatus.KEVENT_REPROCESSING,
    PipelineStatus.MCR_IMPORT_PENDING,
    PipelineStatus.MCR_PUSHED,  # bug du jour : ajouté ici
})

RETRYABLE_TERMINAL: frozenset[PipelineStatus] = frozenset({
    PipelineStatus.KEVENT_FAILED,
    PipelineStatus.MCR_IMPORT_FAILED,
    PipelineStatus.MCR_PUSH_FAILED,
    PipelineStatus.FAILED,
})

TERMINAL_OK: frozenset[PipelineStatus] = frozenset({
    PipelineStatus.KEVENT_COMPLETED,
    PipelineStatus.KEVENT_PARTIALLY_COMPLETED,
    PipelineStatus.COMPLETED,
    PipelineStatus.MCR_TRANSCRIPT_ONLY,
})

TERMINAL_NON_RETRYABLE: frozenset[PipelineStatus] = frozenset({
    PipelineStatus.MCR_AUTH_FAILED,
    PipelineStatus.MCR_REJECTED,
    PipelineStatus.MCR_UNAVAILABLE_ON_SOURCE,
    PipelineStatus.QUARANTINED,
    PipelineStatus.TRANSCODE_FAILED,
    PipelineStatus.DISABLED,
})

# INVARIANT : tout statut DOIT être dans EXACTEMENT un des 4 sets.
# Vérifié au module load.
_all_categorized = NON_TERMINAL | RETRYABLE_TERMINAL | TERMINAL_OK | TERMINAL_NON_RETRYABLE
_all_enum = frozenset(PipelineStatus)
assert _all_categorized == _all_enum, (
    f"Statuts non catégorisés : {_all_enum - _all_categorized}. "
    "Tout membre de PipelineStatus doit être dans EXACTEMENT un des sets "
    "NON_TERMINAL, RETRYABLE_TERMINAL, TERMINAL_OK ou TERMINAL_NON_RETRYABLE."
)
# Disjonction (pas de chevauchement) :
for s in (NON_TERMINAL, RETRYABLE_TERMINAL, TERMINAL_OK, TERMINAL_NON_RETRYABLE):
    others = (NON_TERMINAL | RETRYABLE_TERMINAL | TERMINAL_OK | TERMINAL_NON_RETRYABLE) - s
    assert not (s & others), f"Statut dans 2 sets : {s & others}"
```

### 3.2. Check au build CI

Nouveau script `scripts/check_pipeline_status_consistency.py` :

```python
"""Échoue le build si un statut littéral est trouvé dans le code hors enum.

Lancé en CI (pre-commit + GitHub Actions). Garde-fou contre l'oubli
classique : un dev ajoute "new_status" en code sans le déclarer dans
PipelineStatus → cet outil le détecte avant merge.
"""

import re, sys
from pathlib import Path
from libs.shared.app.pipeline_states import PipelineStatus

REPO = Path(__file__).resolve().parent.parent
SEARCH_DIRS = ["services", "libs"]
PATTERN = re.compile(
    r'transcription_status\s*=\s*["\']([^"\']+)["\']'
    r'|\.transcription_status\s*==\s*["\']([^"\']+)["\']'
)

declared = {s.value for s in PipelineStatus}
violations = []

for d in SEARCH_DIRS:
    for py in (REPO / d).rglob("*.py"):
        if "test" in py.parts:
            continue  # tests peuvent stub des statuts non-canoniques
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            for m in PATTERN.finditer(line):
                value = m.group(1) or m.group(2)
                if value and value not in declared:
                    violations.append(f"{py}:{lineno} — statut non déclaré : {value!r}")

if violations:
    print("Statuts littéraux trouvés hors PipelineStatus :", file=sys.stderr)
    for v in violations:
        print("  " + v, file=sys.stderr)
    print(
        "\nFix : ajouter ces valeurs à libs/shared/app/pipeline_states.PipelineStatus "
        "et les catégoriser dans un des 4 sets (NON_TERMINAL, RETRYABLE_TERMINAL, "
        "TERMINAL_OK, TERMINAL_NON_RETRYABLE). Cf ADR-0002.",
        file=sys.stderr,
    )
    sys.exit(1)

print(f"OK — {len(declared)} statuts déclarés, 0 violation.")
```

### 3.3. Migration progressive

Phase 1 (1 jour) :
- Créer `libs/shared/app/pipeline_states.py` avec l'enum exhaustif
  et les 4 sets
- Faire passer les assert (invariant exhaustivité + disjonction)
- Créer le script CI, l'ajouter au pre-commit hook

Phase 2 (~3 sprints, en mode opportuniste) :
- Au fur et à mesure des fix de bugs : remplacer
  `transcription_status = "..."` par
  `transcription_status = PipelineStatus.XXX.value`
- Remplacer `NON_TERMINAL_STATUSES` (string set local de
  pipeline_watchdog.py) par l'import de `pipeline_states.NON_TERMINAL`
- Idem pour RETRYABLE_TERMINAL

Aucun changement de comportement, juste centralisation. Les sets
locaux deviennent des alias de référence pour transition douce.

## 4. Alternatives considérées et rejetées

### 4.1. Module central `pipeline_state.transition()` (Option B du chantier)

Aurait remplacé tous les `setattr` directs par une fonction unique
qui valide la transition FROM→TO.

**Rejeté pour cet ADR** car (a) demande refactor de tous les
`_set_user_audio_status` + UPDATE SQL bruts, ~3-5 jours, (b) la
plus-value est dans l'instrumentation (Prometheus, alerting) qui
est un besoin moins urgent que la suppression des purgatoires.

Sera réévalué dans 3 mois (ADR successeur potentiel).

### 4.2. Migration vers une job queue (Celery / Temporal)

Aurait éliminé la classe de bug entière (le broker gère
visibility/heartbeat/retry).

**Rejeté pour cet ADR** car refonte majeure (1-2 sprints), nouvelle
dépendance d'infrastructure, ROI incertain à notre échelle actuelle
(~100 transcriptions/jour). À ouvrir si l'échelle change ou si
plusieurs incidents survivent malgré Options A et B.

### 4.3. Status non-typé + tests d'intégration exhaustifs

Aurait gardé les strings littérales mais ajouté une batterie de tests
qui vérifient que chaque statut peut converger.

**Rejeté** car les tests n'auraient pas attrapé les régressions
silencieuses (un statut posé en prod par un endpoint rarement testé).
L'enum + check CI attrapent à l'écriture du code, beaucoup plus tôt.

## 5. Conséquences

### 5.1. Positives

- **Zéro régression future de classe 1** (purgatoire éternel). Tout
  ajout de statut sans catégorisation fail le build, le dev doit
  faire un choix explicite.
- **Documentation auto** : l'enum + les 4 sets sont la spec lisible
  du pipeline. Un nouveau dev/agent comprend la machine à états en
  ouvrant un fichier.
- **Onboarding facilité** : `PipelineStatus.KEVENT_QUEUED` est
  navigable (clic IDE → définition), grep-able, type-checked. Les
  strings littérales `"kevent_queued"` ne le sont pas.
- **Refactoring sûr** : renommer un statut = renommer l'enum + check
  CI confirme que toutes les occurrences sont mises à jour.

### 5.2. Négatives / coûts assumés

- **1 jour d'effort initial** pour créer l'enum + script CI + migrer
  les sets locaux de pipeline_watchdog.py vers l'import.
- **Discipline de revue PR** : il faut accepter que le check CI bloque
  une PR qui ajoute un statut sans catégorisation. Légèrement
  frustrant pour le dev mais c'est exactement le but.
- **Pas de rétro-fix** : les bugs latents actuels (cf scans du
  chantier) ne sont PAS résolus par cet ADR. Doivent être fixés
  séparément (cf §5bis du chantier).

### 5.3. Risques résiduels

- L'enum + script ne couvrent QUE la classe 1 (purgatoires statut).
  Les classes 2, 3, 4 demandent d'autres patterns (cf chantier §3).
  Cet ADR ne prétend pas tout résoudre.
- Le script CI grep avec regex — peut manquer des constructions
  exotiques (`getattr(obj, "transcription_status")` puis assignation
  dynamique). À monitorer.

## 6. Comment cela soutient l'offre produit

Reprend le fil de l'ADR-0001 (séparation liveness/progress comme
fondement de la promesse « ça marche à tous les coups ») et lui
ajoute une **garantie de discipline** : non seulement le pattern est
documenté, mais le build le **fait respecter** automatiquement.

Pour un commanditaire ministériel ou un auditeur sécurité :
- ADR-0001 répond à « comment ça marche quand un audio long est
  traité »
- ADR-0002 répond à « comment vous garantissez que cette promesse
  tient dans le temps, quand l'équipe change ou quand un dev ajoute
  un nouveau cas »

C'est le passage de la propriété **constatée à un moment T** à la
propriété **invariante par construction**. C'est l'argument fort qui
sépare un système artisanal d'un système industrialisable.

## 7. Suivi

| Action | Responsable | Échéance |
|---|---|---|
| Créer `pipeline_states.py` avec enum complet | À assigner | 1 jour |
| Créer `scripts/check_pipeline_status_consistency.py` | À assigner | 1 jour |
| Ajouter au pre-commit hook + CI GitHub Actions | À assigner | 30 min |
| Migrer les sets locaux de pipeline_watchdog.py | À assigner | 1h |
| Fix des 4 items du §5bis du chantier (parallèle) | À assigner | 2h |
| Décision sur ADR successeur (Option B / C) | À planifier | Dans 3 mois |

## 8. Validation

| Élément | Statut |
|---|---|
| Cet ADR (le pattern proposé) | **Proposé** — en attente de revue PO |
| Implémentation Phase 1 (enum + check) | À démarrer |
| Implémentation Phase 2 (migration progressive) | À démarrer en mode opportuniste |
