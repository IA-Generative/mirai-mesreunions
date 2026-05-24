-- 022 — Édition utilisateur : indices des blocs masqués/barrés dans la
-- transcription.
--
-- Permet à l'utilisateur de marquer comme "à supprimer" des blocs de la
-- transcription speaker-tagged via l'éditeur de la fiche réunion (cf
-- services/mesreunions-web/frontend/legacy.js mountTranscriptCorrector).
--
-- Flux :
--   1. User clique "🚫 Barrer" sur un bloc → indice ajouté à
--      hidden_block_indices (visible barré, exclu des exports).
--   2. Plus tard, user clique "🗑 Supprimer les blocs barrés" en en-tête
--      → POST delete-hidden-blocks qui retire vraiment les blocs de
--      speaker_tagged_text + vide hidden_block_indices.
--
-- Les indices référencent les blocs PARSÉS par _parseSpeakerTagged côté
-- frontend (un bloc = une ligne **Speaker** _(MM:SS → MM:SS)_ suivie de
-- ses > lignes de texte). L'ordre est stable tant qu'on ne reprocess pas.
--
-- Idempotent : IF NOT EXISTS.

BEGIN;

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS hidden_block_indices JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;
