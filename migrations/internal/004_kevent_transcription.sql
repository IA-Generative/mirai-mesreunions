-- ============================================================================
-- Migration 004 : pipeline transcription Kevent + meeting-intelligence
--
-- Pourquoi ?
--   On ajoute un troisième backend de transcription (`kevent`, gateway Mirai
--   inférence + LiteLLM pour les étapes de post-traitement). Chaque étape de
--   post-traitement (diarisation, naming, cleaning, reformulation, analyse)
--   est toggleable et stocke sa sortie dans une colonne dédiée. NULL = étape
--   désactivée OU échouée (cf transcription_status).
--
-- Colonnes ajoutées à user_audio_files :
--   transcription_engine    : `stub` | `mcr` | `kevent` — audit du backend qui a écrit
--   transcription_language  : ISO-639-1 détecté par Whisper
--   diarization_json        : segments pyannote bruts
--   speaker_tagged_text     : texte mergé avec speakers (Markdown)
--   cleaned_text            : version OOB-cleaned par LLM
--   reformulated_text       : version discours indirect par LLM
--   meeting_analysis_json   : analyse 5-sections structurée par LLM
--
-- Cible : postgres-internal (database `audio_upload_int`).
-- Idempotent.
-- ============================================================================

\set ON_ERROR_STOP on

BEGIN;

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS transcription_engine    VARCHAR(50),
    ADD COLUMN IF NOT EXISTS transcription_language  VARCHAR(10),
    ADD COLUMN IF NOT EXISTS diarization_json        TEXT,
    ADD COLUMN IF NOT EXISTS speaker_tagged_text     TEXT,
    ADD COLUMN IF NOT EXISTS cleaned_text            TEXT,
    ADD COLUMN IF NOT EXISTS reformulated_text       TEXT,
    ADD COLUMN IF NOT EXISTS meeting_analysis_json   TEXT;

-- Lookup engine in admin views (eg. count rows per backend).
CREATE INDEX IF NOT EXISTS ix_user_audio_engine
    ON user_audio_files (transcription_engine);

COMMIT;

-- Vérification
SELECT
  count(*)                                                                       AS total,
  count(*) FILTER (WHERE transcription_engine = 'stub')                          AS via_stub,
  count(*) FILTER (WHERE transcription_engine = 'mcr')                           AS via_mcr,
  count(*) FILTER (WHERE transcription_engine = 'kevent')                        AS via_kevent,
  count(*) FILTER (WHERE diarization_json IS NOT NULL)                           AS with_diarization,
  count(*) FILTER (WHERE meeting_analysis_json IS NOT NULL)                      AS with_analysis
FROM user_audio_files;
