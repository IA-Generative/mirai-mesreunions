# ADR 0001 — Séparer "liveness du worker" et "progress du traitement" dans le pipeline transcription

| | |
|---|---|
| **Statut** | Accepté (Phase 1 livrée) — Évolutions Phase 2 proposées, blocées sur amont Kevent |
| **Date** | 2026-05-24 |
| **Auteurs** | Eric Tiquet ([@etiquet](https://github.com/etiquet)) + Claude Code |
| **Décideurs** | Eric Tiquet (PO/tech lead), équipe MCR |
| **Sponsors** | Direction du numérique de l'intérieur — promesse produit "transcription fiable bout-en-bout" |
| **Périmètre technique** | `mirai-mesreunions` (ce repo) + asks upstream `IA-Generative/kevent-ai` |
| **Périmètre produit** | Service `mesreunions.fake-domain.name` / offre transcription automatique de réunions ministérielles |
| **Périmètre offre** | Audios attendus 10 min – 2h, langue FR, déploiement souverain (Scaleway fr-par), SSO mirai, niveau de service "ça marche à tous les coups dans un délai borné" |

## 1. Contexte

### 1.1. Promesse produit

L'offre `mes-reunions` engage l'administration sur une transcription
**fiable** : tout audio importé (depuis le mobile PWA, depuis l'upload
web, ou via le bouton "📥 Depuis MCR") doit converger vers un état
terminal exploitable dans un délai borné — **soit un résultat
exploitable, soit un échec explicite avec action utilisateur claire**.
C'est l'engagement de niveau de service sur lequel la diffusion à
l'ensemble des ministères repose. Sans cette propriété, l'offre n'est
pas industrialisable.

### 1.2. Incident déclencheur (mai 2026)

En prod-bêta, sur des audios de réunion réels (60–130 min), 7 lignes
`user_audio_files` étaient bloquées en `kevent_failed` avec
`reprocess_version` entre 49 et 61. Le bouton "Relancer les sujets
bloqués" relançait sans effet — la transcription échouait
systématiquement après ~5 minutes, peu importe la qualité de l'audio.
L'engagement de service n'était pas tenu.

### 1.3. Investigation et cause racine

Une investigation conjointe (lecture code + analyse logs + sondes DB +
reproduction Kevent manuelle) a démontré que le bug n'était **pas
dans Kevent** (les bugs A/B/C catalogués — timeout 300s relay pyannote,
samples mismatch AAC, TTL court gateway — ne reproduisaient pas) mais
dans **notre propre `pipeline_watchdog`** :

- Le watchdog utilise une colonne unique `last_activity_at` pour
  décider "ce job est orphelin → reprendre".
- Le callback `on_status` de notre client Kevent ne notifie que les
  **changements de phase** (queued → processing → completed).
- Pour un audio de 2h transcrit en ~6 min côté Kevent, la phase
  `processing` reste stable pendant les 6 min entières → pas de
  refresh de `last_activity_at` → le watchdog (seuil 300s) considère
  la row stale et la "vole" : republie un nouveau job, génère un
  nouveau `kevent_job_id`. L'ancien job Kevent continue côté GPU en
  parallèle ; son résultat est jeté à la livraison.
- Cycle infini jusqu'au cap `MAX_AUTO_RETRIES=5` (commit `3cb2ad7`,
  pansement déployé peu avant l'incident).

Preuves convergentes :

- Tous les `(completion_at - started_at) ≈ 300s` exactement, pas la
  durée Kevent réelle.
- `reprocess_history` 100 % `type=watchdog`, `prev_status=kevent_queued`.
- Audio probe manuel sur Kevent → `job not found` (TTL expiré côté
  gateway) — l'ancien job avait bien tourné, son résultat avait été
  produit et expiré sans être lu.

### 1.4. Pourquoi ça n'a pas été vu en design initial

Le watchdog était conçu pour la **récupération après crash** : "si un
pod meurt en plein pipeline, un autre pod doit pouvoir reprendre".
Cette propriété est correcte pour des opérations de durée bornée
(< `STALE_THRESHOLD_S=300s`) — ce qui était le cas pendant la phase
de mise au point (audios de test de quelques minutes). À la mise en
production avec des réunions réelles (60–130 min), la condition de
durée s'inverse : la transcription dure plus longtemps que le seuil
de détection d'orphelin. **Le watchdog cesse de protéger contre le
crash et devient lui-même la cause de la panne.**

C'est un classique des systèmes distribués : un seul signal capté
("pas d'activité DB") pour deux phénomènes que tout oppose ("worker
mort" vs "worker occupé"). À l'échelle, la confusion produit un
livelock.

## 2. Décision

Nous adoptons une décomposition explicite des deux signaux que le
watchdog actuel mélange :

| Signal | Question répondue | Source de vérité | Action de remédiation |
|---|---|---|---|
| **Liveness du worker** | Est-ce que le processus qui tient ce job est toujours en vie ? | Lease renouvelé par le client à un rythme fixe (3s aujourd'hui) | Reclaim du job si pas de signal pendant > N intervalles |
| **Progress du traitement** | Est-ce que le moteur (Whisper/pyannote) avance réellement ? | Changement de phase explicite (queued → processing → completed) | Aucune — pas de remédiation côté ingester, seulement reporting UI |

### 2.1. Phase 1 (livrée, ce repo, mai 2026)

Quatre changements client cumulés, déployés ensemble :

1. **Heartbeat de liveness sur chaque poll Kevent** (commit `78abecb`) :
   ajout d'un callback `on_poll(status)` dans
   `kevent_client.wait_for_job()`, appelé à chaque tick (3s) qu'il y
   ait changement de phase ou non. Le callback déclenche un UPDATE
   `last_activity_at=NOW()` léger (1-shot, sans toucher d'autres
   colonnes). Ferme la fenêtre de "vol" par le watchdog.

2. **Reset DB explicite avant republish MCR** (`_resume_mcr_import`,
   commit `78abecb`) : aligne le path MCR sur le pattern Kevent
   (UPDATE statut + bump `reprocess_version` + append
   `reprocess_history`) qui était absent et causait une boucle MCR
   distincte.

3. **Garde inter-worker du watchdog** (flock POSIX, commit `78abecb`) :
   un seul watchdog par pod gunicorn, plus N par pod (un par worker
   pre-fork).

4. **Observabilité erreurs** (migration 020 + commit `78abecb` +
   `6c63c35`) : colonnes `last_error_at/kind/message`, peuplées par
   tous les `_set_failed` / `_set_user_audio_status` /
   `_mark_capped_as_failed`. UI surface un message humanisé par kind
   avec action utilisateur explicite. Détection `s3_object_purged`
   et `s3_no_audio_path` comme statuts terminaux non-relançables.

### 2.2. Phase 2 (proposée, à porter côté Kevent gateway)

Trois propositions consignées dans
[`docs/upstream-asks/kevent/`](../upstream-asks/kevent/) pour porter
le mécanisme côté broker :

1. **Bug report** : démonstration de la fuite GPU côté Kevent quand
   le client republie sans signaler l'abandon.
2. **PR `DELETE /jobs/{service_type}/{id}`** : permet au client
   bien-intentionné de libérer explicitement le slot avant
   resubmission.
3. **RFC lease auto-renouvelé par GET** : défense en profondeur pour
   les clients crashés ou en partition réseau. Pattern SQS / Celery /
   Temporal / Kubernetes — prior art éprouvée.

Phase 2 **n'est pas bloquante** pour tenir la promesse produit côté
client : Phase 1 ferme le bug observable. Phase 2 ajoute la défense
en profondeur côté plateforme et économise du GPU à l'échelle multi-
tenant.

### 2.3. Phase 3 (déférée, sprint dédié)

Refonte du pipeline `_transcribe_via_kevent` monolithique en
sous-fonctions composables avec :

- Timeouts différenciés par étape (5s pour crash-recovery,
  30 min pour transcription)
- Idempotence du republish (rejoindre `resume_job` si on a déjà un
  `kevent_job_id` au lieu de re-soumettre)
- Métriques Prometheus dédiées (cf §6 du
  [runbook](../RUNBOOK_PIPELINE_RELIABILITY.md))

Trackée dans la mémoire utilisateur
`feedback_pipeline_monolithic_antipatterns` + plan
`~/.claude/plans/federated-finding-whisper.md`. Pas urgente,
Phase 1 ferme la fuite.

## 3. Alternatives considérées et rejetées

### 3.1. Augmenter `STALE_THRESHOLD_S` à 1800s (30 min)

**Rejetée.** C'est un déplacement du seuil, pas une correction. Sur
un audio de 4h (qui arrivera tôt ou tard côté ministériel), on
reproduirait le bug. De plus, allonger le seuil dégrade le délai de
détection des vrais crashs — on revient à la confusion liveness vs
progress, juste avec une fenêtre plus large.

### 3.2. Supprimer purement le watchdog

**Rejetée.** Sans watchdog, un pod tué en plein pipeline laisse la
row coincée indéfiniment. La récupération après crash reste un
besoin légitime. La décomposition (Phase 1) le préserve.

### 3.3. Implémenter un "tu as avancé ?" sémantique côté gateway

**Rejetée.** Inspecter si Whisper/pyannote "avance" est très
difficile : ces moteurs ne livrent pas de progress intermédiaire
exploitable, ils prennent un audio en entrée et un résultat en
sortie. Mesurer l'occupation GPU ne distingue pas "transcrit
activement" de "boucle infinie sur un bug numpy". La métrique de
progrès du modèle est une promesse qu'on ne peut pas tenir
correctement ; vaut mieux ne pas la promettre.

### 3.4. Acheter plus de GPU pour absorber la fuite

**Rejetée.** Mauvaise réponse à un bug logiciel. Mais surtout : la
fuite **augmente avec le nombre d'utilisateurs** (plus de boucles
en parallèle), donc scale horizontalement le problème au lieu de le
résoudre. À l'échelle de la diffusion ministérielle, intenable.

## 4. Conséquences

### 4.1. Positives

- **Pour l'offre produit** : la promesse "ça marche à tous les coups
  dans un délai borné" est tenue côté infrastructure. Toute ligne
  converge vers un statut terminal explicite ; tout échec terminal
  porte une cause utilisateur (`last_error_kind`) et une action
  recommandée (UI). L'engagement de niveau de service est défendable
  devant un client ministériel.
- **Pour les utilisateurs** : disparition du "ça reste bloqué et je
  ne sais pas quoi faire". Les messages d'erreur deviennent
  actionnables.
- **Pour les opérations** : un runbook explicite ([RUNBOOK_PIPELINE_RELIABILITY.md](../RUNBOOK_PIPELINE_RELIABILITY.md))
  fournit les requêtes SQL et commandes kubectl de diagnostic. Les
  régressions ont des signaux clairs.
- **Pour Kevent (à terme)** : si Phase 2 est portée upstream,
  économie de GPU mesurable (~40 GPU-hours par incident type évité)
  + protection des autres tenants Kevent qui reproduiraient le bug
  client.

### 4.2. Négatives / coûts assumés

- **Une écriture DB supplémentaire toutes les 3s par job Kevent en
  cours.** Sur 100 jobs concurrents, ~33 UPDATE/s. Négligeable vs
  le coût d'un job stale repris à tort (qui déclenchait 6 UPDATE +
  une re-submission GPU).
- **Migration SQL 020 à déployer AVANT le rollout** — risque
  opérationnel maîtrisé par le runbook et la doc memory
  `feedback_migration_before_rollout`.
- **Phase 2 requiert une discussion + coordination avec une équipe
  externe (Kevent).** Le calendrier dépend de leur disponibilité.
  Phase 1 ne dépend pas de Phase 2.
- **Service worker PWA agressivement caché** — les utilisateurs
  doivent faire un hard refresh pour voir les nouveaux messages
  d'erreur. À communiquer à la diffusion.

### 4.3. Risques résiduels

- **Long audios > MAX_RTF × budget Kevent** (cas hypothétique : audio
  de 10h). Aujourd'hui le `KEVENT_ASYNC_TIMEOUT_SECONDS=0` (pas de
  plafond), donc on attendrait indéfiniment. La Phase 2 (RFC budget
  par audio_duration) couvre ce cas. À surveiller en attendant.
- **Multi-pod : si le flock `/tmp/pipeline_watchdog.lock` est posé
  sur un volume partagé entre pods** (ce qui n'est pas le cas
  aujourd'hui, mais pourrait l'être si on remonte des ConfigMap), le
  comportement serait incorrect (LOCK_NB échoue entre pods). À
  documenter clairement, contrôlé par variable d'environnement
  `PIPELINE_WATCHDOG_LOCK_PATH`.
- **Le cap `MAX_AUTO_RETRIES=5` reste applicable** — un utilisateur
  qui clique "Relancer" 6 fois sur une row vraiment cassée verra
  toujours `cap_exceeded`. C'est le comportement voulu, et le
  message est désormais actionnable.

## 5. Comment cela soutient l'offre produit

L'offre `mes-reunions` est construite sur trois piliers :

1. **Souveraineté** : déploiement Scaleway fr-par, SSO mirai, pas de
   dépendance externe non-souveraine.
2. **Confidentialité** : audio stocké en S3 internal chiffré,
   isolation des données utilisateur par `user_sub` OIDC,
   purge automatique configurable.
3. **Fiabilité** : ce qui est ici en jeu.

Avant cet ADR, le pilier #3 n'était pas tenable à grande échelle.
L'incident de mai 2026 aurait été un point d'arrêt à la diffusion
ministérielle (un agent qui ne peut pas faire transcrire sa
réunion ne réutilise pas le service). La décomposition
liveness/progress, livrée en Phase 1 et formalisée par cet ADR :

- **Permet d'industrialiser l'offre** sur des audios réels
  (60 min – 2 h) sans régression de fiabilité.
- **Rend l'échec lisible à l'utilisateur** quand il survient, plutôt
  qu'un statut "en cours" qui dure indéfiniment — l'utilisateur
  garde le contrôle (relancer / supprimer / contacter support).
- **Donne aux opérations le langage** pour discuter SLO et incidents
  (statuts terminaux explicites, codes d'erreur catalogués, runbook
  de diagnostic).
- **Cadre les évolutions futures** : la Phase 2 (lease Kevent
  amont) et la Phase 3 (refonte pipeline monolithique) ont un fil
  conducteur clair et défendable.

C'est sur cette base qu'on peut s'engager devant un commanditaire
ministériel sur un SLA de "convergence sous 4h pour un audio de
< 2h, échec explicite sinon". Avant l'ADR, on faisait du best-effort.
Après, on a un contrat.

## 6. Suivi

| Action | Responsable | Échéance |
|---|---|---|
| Déploiement Phase 1 prod-bêta | Eric Tiquet | Immédiat (post-merge branche) |
| Communication hard refresh aux utilisateurs prod-bêta | Eric Tiquet | Concurrent au deploy |
| Soumission issue Kevent #1 (bug report) | Eric Tiquet | S+1 post-deploy |
| Soumission PR Kevent #2 (DELETE endpoint) | À discuter avec équipe Kevent | Selon réponse à #1 |
| Soumission RFC Kevent #3 (lease) | À discuter avec équipe Kevent | Selon traction de #2 |
| Sprint refonte pipeline (Phase 3) | À planifier | Pas avant retour de prod-bêta sur Phase 1 |
| Métriques Prometheus dédiées | À planifier (sprint observabilité) | À cadrer |

## 7. Références

- Investigation initiale et plan : `~/.claude/plans/valiant-fluttering-nest.md`
- Brief produit déclencheur : `~/.claude/plans/ok-maintenant-nouveau-sujet-calm-avalanche.md`
- Runbook opérationnel : [`docs/RUNBOOK_PIPELINE_RELIABILITY.md`](../RUNBOOK_PIPELINE_RELIABILITY.md)
- Drafts asks upstream : [`docs/upstream-asks/kevent/`](../upstream-asks/kevent/)
- Migration SQL : [`migrations/internal/020_user_audio_last_error.sql`](../../migrations/internal/020_user_audio_last_error.sql)
- Mémoires utilisateur pertinentes :
  - `project_pipeline_watchdog`
  - `feedback_pipeline_monolithic_antipatterns`
  - `project_kevent_long_audio_limits`
  - `feedback_migration_before_rollout`
  - `feedback_in_memory_state_multi_replicas`
  - `feedback_manifest_prod_divergence`
- Patterns d'industrie référencés :
  - SQS visibility timeout
  - Celery `visibility_timeout` + `acks_late`
  - Temporal activity heartbeats
  - Kubernetes coordination.k8s.io/Lease

## 8. Statut de validation

| Phase | Statut | Validé par |
|---|---|---|
| **Phase 1** (4 commits livrés sur `feat/import-from-mcr`) | Accepté | Auteur ADR + investigation Phase 0 cross-validée par agent autonome |
| **Phase 2** (3 drafts upstream) | Proposé | En attente discussion équipe Kevent |
| **Phase 3** (sprint refonte) | Différé | En attente retours prod-bêta Phase 1 |

Cet ADR est révisable. Toute évolution majeure du modèle
liveness/progress (par exemple, l'arrivée d'un broker tiers comme
Temporal côté plateforme) déclenche un ADR successeur qui supersede
celui-ci.
