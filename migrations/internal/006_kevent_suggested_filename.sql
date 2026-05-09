-- ============================================================================
-- Migration 006 : métadonnées LLM (titre + points clés) pour les exports
--
-- Pourquoi ?
--   À l'export downloads (transcription, CR), l'utilisateur préfère retrouver
--   un nom de type « Réunion budget Q3 2026-05-09.docx » plutôt que
--   `voxpop_001.txt`, et la liste UI bénéficie d'un résumé court par fichier.
--   Un seul appel LLM léger (chat-small) produit les deux infos en JSON
--   structuré, on les stocke dans deux colonnes. La date est ajoutée par le
--   code-generator au téléchargement.
--
-- Toggle : KEVENT_FILENAME_SUGGESTION_ENABLED. NULL si désactivé OU si
--   l'appel LLM a échoué (le code-generator retombe sur original_filename
--   et l'UI n'affiche pas de subtitle).
--
-- Cible : postgres-internal (database `audio_upload_int`).
-- Idempotent.
-- ============================================================================

\set ON_ERROR_STOP on

BEGIN;

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS suggested_filename VARCHAR(255),
    ADD COLUMN IF NOT EXISTS key_points_summary TEXT;

COMMIT;

SELECT
  count(*)                                                AS total,
  count(*) FILTER (WHERE suggested_filename IS NOT NULL)  AS with_suggested_filename,
  count(*) FILTER (WHERE key_points_summary IS NOT NULL)  AS with_key_points
FROM user_audio_files;
