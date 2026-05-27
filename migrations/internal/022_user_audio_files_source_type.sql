-- 022 — user_audio_files : source_type + external_video_source_id
--
-- Permet d'absorber les imports YouTube (et futurs MCR / DINUM /
-- multi-format) dans la même table que les uploads classiques,
-- court-circuit des étapes scanning/transcoding/transfer/whisper en
-- pré-remplissant directement `transcription_text` depuis le
-- transcript récupéré par le connecteur externe.
--
-- Plan : ~/.claude/plans/l-importation-de-fichier-youtube-nifty-frost.md (C1)
-- Migration ADDITIVE et idempotente — rejouable sans effet.
--
-- Aucun rollback DOWN nécessaire : les rows existantes restent en
-- 'upload' (DEFAULT), aucun champ n'est supprimé, stored_filename
-- devient nullable mais les INSERT existants continuent de marcher.

BEGIN;

-- ENUM des sources possibles. 'upload' couvre l'historique
-- (audios poussés via PWA/local). 'youtube_subtitle' = sous-titres
-- récupérés par video-ingest sans audio. 'youtube_audio' = audio
-- téléchargé puis transcrit par Kevent (force_audio=true).
-- À étendre pour MCR (V4 : 'external_transcript' + colonne provider)
-- et DINUM (V5).
DO $$ BEGIN
  CREATE TYPE audio_source_type AS ENUM (
    'upload',
    'youtube_subtitle',
    'youtube_audio'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE user_audio_files
  ADD COLUMN IF NOT EXISTS source_type audio_source_type NOT NULL DEFAULT 'upload';

ALTER TABLE user_audio_files
  ADD COLUMN IF NOT EXISTS external_video_source_id BIGINT;

-- stored_filename devient nullable pour les sources YouTube (pas de
-- fichier S3 derrière). Les UPLOAD continuent de l'avoir non-NULL via
-- le code applicatif (constraint applicative, pas DB).
DO $$ BEGIN
  ALTER TABLE user_audio_files ALTER COLUMN stored_filename DROP NOT NULL;
EXCEPTION
  WHEN others THEN
    -- Si déjà nullable, ALTER échoue côté postgres sur certaines versions —
    -- on ignore pour garder l'idempotence.
    NULL;
END $$;

CREATE INDEX IF NOT EXISTS ix_uaf_source_type
  ON user_audio_files (source_type)
  WHERE source_type <> 'upload';

CREATE INDEX IF NOT EXISTS ix_uaf_external_video_source
  ON user_audio_files (external_video_source_id)
  WHERE external_video_source_id IS NOT NULL;

COMMIT;
