-- 017 — Ajoute la colonne ``transcription_words_json`` à user_audio_files
-- pour stocker les word-level timestamps Whisper (mode word_timestamps=true).
--
-- Contexte : Whisper renvoie désormais, avec ``word_timestamps=true``, un
-- champ ``words: [{word, start, end, probability}]`` dans chaque segment
-- verbose_json. Le frontend mesreunions-web l'utilise pour le surlignage
-- karaoke du mot prononcé pendant la lecture audio (cf. tc-block dans
-- legacy.js > mountTranscriptCorrector).
--
-- Format stocké : JSON array compact aplati de tous les words de tous
-- les segments, ordonné par ``s`` croissant :
--   [{"w": "Bonjour", "s": 1.23, "e": 1.78}, ...]
-- Aucune assignation de speaker à ce niveau — le frontend joint avec
-- les blocs (speaker_tagged_text) via les timestamps.
--
-- Idempotente : ``IF NOT EXISTS`` protège contre un re-run.
-- NULL pour toutes les lignes existantes (rétro-compatible : si NULL,
-- le frontend dégrade au highlight par bloc, comportement actuel).
--
-- ⚠️ Cette migration DOIT être appliquée AVANT le rollout du service
-- ``dmz-to-internal-bridge`` qui écrit la colonne (cf. memoire
-- feedback_migration_before_rollout — sinon UndefinedColumn en boucle).

BEGIN;

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS transcription_words_json TEXT;

COMMENT ON COLUMN user_audio_files.transcription_words_json IS
    'Whisper word-level timestamps (compact JSON array). NULL si word_timestamps non émis. Format: [{"w":..., "s":..., "e":...}]';

COMMIT;
