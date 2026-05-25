-- 021 — video-ingest : journal d'audit (Q5).
--
-- Trace immuable des actions sensibles : import lancé (HIT/MISS),
-- purge admin, bascule force_audio. Indispensable côté conformité
-- (qui a importé quoi quand) et utile au debug post-mortem.
--
-- Pas de FK vers video_sources (D14 + résilience : on garde la trace
-- même après purge de la source). `video_source_id` est un pointeur
-- soft.
--
-- Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS video_ingest_audit (
    id              BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_sub        VARCHAR(255) NOT NULL,
    action          VARCHAR(32)  NOT NULL,    -- import | purge | force_audio | error
    url             TEXT,                      -- URL brute soumise (si applicable)
    video_source_id BIGINT,                    -- soft pointer
    reused          BOOLEAN,                   -- import : HIT/MISS
    job_id          BIGINT,                    -- soft pointer vers video_ingest_jobs
    context         VARCHAR(32),
    context_id      VARCHAR(128),
    details_json    JSONB        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_video_ingest_audit_user
    ON video_ingest_audit (user_sub, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_video_ingest_audit_action
    ON video_ingest_audit (action, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_video_ingest_audit_source
    ON video_ingest_audit (video_source_id)
    WHERE video_source_id IS NOT NULL;

COMMIT;
