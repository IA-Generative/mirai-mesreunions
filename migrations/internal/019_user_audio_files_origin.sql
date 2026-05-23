-- 019 — user_audio_files.origin : distinguer la provenance d'un enregistrement
-- (upload depuis mesreunions-web, upload mobile via PWA, import depuis MCR).
--
-- Sert le feature "Importer une réunion depuis MCR" : on insère une ligne avec
-- origin='mcr_import' au moment où l'utilisateur coche une réunion dans la
-- modale d'import, puis le worker mcr_importer asynchronise la récupération
-- audio/transcription.
--
-- Idempotent : IF NOT EXISTS partout.

BEGIN;

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS origin VARCHAR(20) NOT NULL DEFAULT 'upload';

-- Backfill explicite pour les uploads venant du mobile (origine = présence
-- d'un device_id sur la session) : best-effort, le défaut reste 'upload'.
UPDATE user_audio_files uaf
SET origin = 'mobile'
FROM upload_sessions us
WHERE us.id = uaf.session_id
  AND us.device_id IS NOT NULL
  AND uaf.origin = 'upload';

-- Dédoublonnage : un user ne ré-importe pas deux fois la même réunion MCR.
-- Index partiel (n'indexe que les lignes mcr_import avec un mcr_meeting_id).
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_audio_files_mcr_import_uniq
    ON user_audio_files (user_sub, mcr_meeting_id)
    WHERE origin = 'mcr_import' AND mcr_meeting_id IS NOT NULL;

COMMIT;
