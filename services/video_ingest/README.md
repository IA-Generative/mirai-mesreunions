# `video-ingest` — service mutualisé d'ingestion vidéo (V1 en cours)

> Spec complète : [`features-2-build/FEATURE_video-ingest.md`](../../features-2-build/FEATURE_video-ingest.md).

Composant **autonome et extrayable** : il vit temporairement dans ce
repo (en cours de rename `mirai-mesreunions` → `mirai-mesreunions`)
mais sera transféré dans son propre dépôt dès maturité V1 (cf. décision
D14). Mes Réunions et Mes Collections sont des **clients** qui parlent
au service par API REST/MCP, jamais par import Python.

## Règles d'isolation (à respecter à chaque PR)

1. **Aucun import** depuis `libs.shared.*` ni depuis un autre `services/*`.
   Les utilitaires nécessaires sont recopiés ici (au pire dupliqués),
   pas mutualisés via le code existant.
2. **Aucune FK** vers les tables MirAI (`user_audio_files`, `meetings`,
   `briefs`, …). Toutes les tables du service sont préfixées `video_*`
   et l'identité utilisateur est un `user_sub` opaque (sub OIDC).
3. **Schéma BDD séparable** : aujourd'hui dans `migrations/internal/019_*`
   pour cohabiter avec le reste, mais regroupable d'un bloc sans devoir
   trier ligne par ligne.
4. **Interface = REST + MCP uniquement**. Pas de signal, pas de bus
   partagé, pas de table commune.
5. **Pas de nouvelle dépendance d'orchestration** : la file de jobs est
   en Postgres natif (`SELECT … FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY`),
   cf. D13.

**Critère de sortie** : `git filter-repo --path services/video_ingest/ --path migrations/internal/019_video_ingest_initial.sql --path tests/unit/test_video_ingest_*.py` doit produire un repo viable, sans référence dangling au reste.

## Nom du package Python

Le dossier est `services/video_ingest/` (underscore) — exception
assumée par rapport au reste du repo (où les services sont en
`kebab-case`) pour rester importable directement en Python sans
gymnastique `importlib`, et pour préfigurer le futur package
`video_ingest` dans son propre repo.

## État courant

| Brique | Statut |
|---|---|
| Schéma BDD initial (migration 019) | ✅ posé |
| Parseur URL YouTube (dédup) | ✅ posé + tests |
| Provider YouTube (metadata + sous-titres + audio) | ⏳ à faire |
| Worker Postgres-native | ⏳ à faire |
| API REST `/import`, `/transcripts/{id}`, `/search` | ⏳ à faire |
| Outils MCP V1 (5 tools) | ⏳ à faire |
| Intégration client Mes Réunions | ⏳ à faire |

Voir le journal d'itération dans la spec pour le détail.
