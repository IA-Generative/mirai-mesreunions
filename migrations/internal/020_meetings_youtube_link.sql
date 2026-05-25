-- 020 — Lien Meeting → video-ingest.
--
-- Pour les Meetings créés depuis l'import YouTube (cf. INTEGRATION_NOTES.md §1
-- côté services/video_ingest/), on stocke un pointeur **opaque** vers la
-- source video-ingest. Pas de FK cross-service (D14 — video-ingest est
-- destiné à être extrait dans son propre repo).
--
-- - `video_source_id`     : id de video_ingest.video_sources (transparent, pas de FK)
-- - `video_ingest_job_id` : id de video_ingest.video_ingest_jobs en cours, pour le poll
--
-- Idempotent : IF NOT EXISTS partout.

BEGIN;

ALTER TABLE meetings
    ADD COLUMN IF NOT EXISTS video_source_id     BIGINT,
    ADD COLUMN IF NOT EXISTS video_ingest_job_id BIGINT;

-- Index utile pour lister les meetings d'une source donnée (purge en cascade
-- côté UI, debug, dédup).
CREATE INDEX IF NOT EXISTS ix_meetings_video_source
    ON meetings (video_source_id)
    WHERE video_source_id IS NOT NULL;

COMMIT;
