-- 018 — Pipeline watchdog : colonnes pour détecter et reprendre les
-- jobs orphelins (pod tué en plein traitement, poll loop perdu, etc).
--
-- Cf services/dmz-to-internal-bridge/app/watchdog.py.
--
-- Idempotent : IF NOT EXISTS partout, rejouable sans effet.

BEGIN;

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ;
ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS pipeline_claim_at TIMESTAMPTZ;
ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS pipeline_claim_pod VARCHAR(128);

-- Backfill last_activity_at pour les rows existantes : prend
-- transcription_completed_at si présent, sinon transcription_started_at,
-- sinon created_at. Évite que le watchdog voit toutes les vieilles rows
-- comme orphelines au premier passage.
UPDATE user_audio_files
   SET last_activity_at = COALESCE(transcription_completed_at,
                                    transcription_started_at,
                                    created_at)
 WHERE last_activity_at IS NULL;

-- Index hot path watchdog (scan jobs non-terminaux avec activité ancienne).
CREATE INDEX IF NOT EXISTS ix_user_audio_watchdog
    ON user_audio_files (transcription_status, last_activity_at);

COMMIT;
