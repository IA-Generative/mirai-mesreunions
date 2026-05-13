-- Persiste le job_id Kevent (whisper ou pyannote) associé à chaque fichier
-- audio. Permet :
--   1. À l'UI mydevices de demander la position d'attente précise du fichier
--      via /api/queue-status?job_id=<id>.
--   2. À file-puller de reprendre automatiquement un poll Kevent orphelin
--      au redémarrage du pod (OOM, scale-down, rollout) sans relancer le job.
--
-- Idempotent : "ADD COLUMN IF NOT EXISTS" (PostgreSQL 9.6+).

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS kevent_job_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_uaf_kevent_job_id
    ON user_audio_files (kevent_job_id)
    WHERE kevent_job_id IS NOT NULL;
