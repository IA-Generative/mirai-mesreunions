# ADR-0003 — Meeting Source Connectors (pattern d'ingestion universel)

## Statut
Accepté — 2026-05-27

## Contexte

La V1 d'import YouTube (commits `de1fd07…797b23c` puis `9242db0…6d64108`) a fait apparaître deux constats :

1. Le pipeline meeting-intelligence (`puller._run_llm_chain_for_audio`) est **text-in → dict-out** : glossary_correction, cleaning, reformulation, meeting_analysis, suggest_metadata, key_points_summary. Ces étapes sont **source-agnostiques** et entièrement réutilisables.
2. Une intégration directe par source (cas hardcodé `mcr_import/`, V0 YouTube cloisonnée dans `video_ingest.video_transcripts`) crée immédiatement une dette de couplage : chaque nouvelle source nécessite N modifications cross-services + duplique des bouts d'orchestration LLM.

Plusieurs sources à intégrer à court / moyen terme : YouTube (livré), MCR (`compte-rendu.mirai.fake-domain.name`), Dictaphone DINUM, demain Vimeo / Dailymotion / SRT collés / podcast RSS / multi-format audio bulk. Les recoder à la main une par une = perte sèche.

## Décision

On adopte le pattern **Meeting Source Connector** : tout mode d'apport de contenu dans MirAI est traité comme une instance du même pattern.

- Chaque source = un **connecteur isolé** (Python package ou service dédié) qui respecte le contrat partagé décrit dans `docs/contract/meeting-source-connector-spec.md`.
- Les connecteurs s'authentifient et récupèrent les contenus à leur charge (transcript + segments + audio optionnel).
- Ils notifient le monorepo via un **hook unique** `POST /api/v1/external-source/materialize` (côté `dmz-to-internal-bridge`).
- Cet endpoint crée un `user_audio_files` « virtuel » (status=`kevent_processing`, transcription_text pré-rempli) et **lance le pipeline LLM existant sans modification**.
- Le résultat post-pipeline (CR, glossaire, résumé, suggested_filename) apparaît dans la liste meetings unifiée et la fiche détail standard.

YouTube est le premier connecteur livré et joue le rôle de **référence d'implémentation** du contrat.

## Alternatives écartées

### Option B — Orchestrateur séparé par source
Chaque connecteur recoderait sa propre orchestration LLM, soit en duplicant `_run_llm_chain_for_audio` (~750 lignes), soit en appelant des endpoints REST cross-service.

Rejetée parce que :
- Duplication massive de code éprouvé.
- Couplage opposé à D14 (video-ingest dépendrait du résultat synchrone d'internal-ingester pour faire son boulot).
- Pas de gain : on ré-invente la roue à chaque source.

### Option C — Refactor `_run_llm_chain_for_audio` en lib pure
Extraire toutes les étapes LLM en fonctions text-in → dict-out indépendantes de SQLAlchemy + `user_audio_files`. Chaque service appelle directement la lib.

Reportée parce que :
- ~400 lignes à démêler du puller (gestion statuts, watchdog, persistence) — risque de régression important.
- Le bénéfice principal (suppression de l'appel HTTP cross-service) est modeste vu que materialize tourne sur le même cluster que video-ingest.
- À reconsidérer quand un 3ᵉ point d'appel émergera et que l'overhead du proxy HTTP deviendra problématique.

## Conséquences

### Positives
- **Évolutivité** : ajouter une source = écrire un connecteur respectant le contrat. ~1-2 jours pour un nouveau provider après stabilisation.
- **D14 préservé** : chaque connecteur reste extractable dans son propre repo (video-ingest est déjà l'archétype).
- **Pipeline LLM stable** : zéro modification du puller, donc zéro risque de régression sur le chemin audio classique.
- **UI unifiée** : la liste meetings et la fiche détail acceptent toutes les sources via les mêmes routes/templates.
- **Test conformité partagé** : `tests/contract/test_meeting_connector_contract.py` (V4) garantira que tout connecteur respecte le contrat.

### Négatives
- Coût initial de pose du contrat (sections 1-10 de `docs/contract/meeting-source-connector-spec.md`).
- **Couplage du contrat** : tout changement majeur impacte tous les connecteurs simultanément (mitigé par semver + tests contrat).
- Renommage de schéma à porter pour cohérence (migration 023+ : `video_source_id` → `source_resource_id` + `source_provider`).
- Pendant la transition MCR (V4) : coexistence ancien (`mcr_import/`) + nouveau, à décommissionner en V4.7.

## Périmètre livré (cette V1)

Slices C0 → C7 :
- C0 — script `tests/run-regression-campaign.sh` + baseline 654 tests
- C1 — migration 022 : `user_audio_files.source_type` + `external_video_source_id`
- C2 — modèle SQLAlchemy + serializer
- C3 — endpoint `POST /api/v1/external-source/materialize` + helpers purs (`flatten_segments_to_synthetic_words`, `validate_materialize_payload`, `materialize_payload_to_uaf_kwargs`)
- C4 — hook `video-ingest/orchestrator.py` best-effort vers materialize
- C5 — placeholder Meeting immédiat dans `youtube_import/routes.py` + endpoint `PATCH /api/v1/meetings/<id>/link-video`
- C6 — `GET /api/v1/meetings?with_audio_preview=1` + enrichissement `/api/youtube/my-imports`
- C7 — `renderYoutubeRow` status dynamique + SVG YouTube + open-detail si done

29+7+13 nouveaux tests, 654/654 verts, 0 régression sur la baseline historique.

## Liens

- Plan d'implémentation : `~/.claude/plans/l-importation-de-fichier-youtube-nifty-frost.md`
- Contrat normatif : `docs/contract/meeting-source-connector-spec.md`
- Architecture : `docs/architecture/meeting-source-connectors.md`
- Connecteur de référence : `docs/connectors/youtube.md`
- Spec feature : `features-2-build/FEATURE_video-ingest.md`
- ADR liés : ADR-0001 (pipeline liveness vs progress), ADR-0002 (status enum)
