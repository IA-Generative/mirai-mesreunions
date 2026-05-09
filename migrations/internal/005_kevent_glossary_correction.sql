-- ============================================================================
-- Migration 005 : étape glossaire dans le pipeline Kevent
--
-- Pourquoi ?
--   On ajoute une étape post-transcription (entre speaker_naming et oob_cleaning)
--   qui demande au LLM de corriger les sigles administratifs (Ministère de
--   l'Intérieur etc.) que Whisper transcrit phonétiquement (« deux M L F D I »
--   → « 2MLFDI »). Le glossaire général est embarqué dans l'image file-mover
--   (répertoire `glossaire/` du repo).
--
--   Le résultat est stocké dans une colonne dédiée pour permettre l'audit
--   « qu'est-ce que la correction a changé ? » sans toucher aux autres outputs.
--
-- Toggle : KEVENT_GLOSSARY_CORRECTION_ENABLED (env var). NULL si désactivé OU
--   si aucun terme du glossaire n'a été matché OU si l'appel LLM a échoué.
--
-- Cible : postgres-internal (database `audio_upload_int`).
-- Idempotent.
-- ============================================================================

\set ON_ERROR_STOP on

BEGIN;

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS glossary_corrected_text TEXT;

COMMIT;

SELECT
  count(*)                                                          AS total,
  count(*) FILTER (WHERE glossary_corrected_text IS NOT NULL)       AS with_glossary_correction
FROM user_audio_files;
