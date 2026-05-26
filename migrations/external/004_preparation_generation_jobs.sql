-- 004 — preparation_generation_jobs : store partagé multi-pod pour les jobs
-- de génération de brief (POST /api/preparations).
--
-- Avant : in-memory dict côté mesreunions-web → avec 2+ replicas et
-- sessionAffinity flakey, le polling tombait parfois sur un pod différent
-- du worker → "Job de génération introuvable (expiré ?)".
--
-- Après : DB centralisée → tous les pods voient l'état du job en temps réel.
-- TTL applicatif (1h) maintenu via gc explicit ; index sur finished_at
-- pour accélérer la purge.
--
-- Idempotent : IF NOT EXISTS partout, rejouable sans effet.

BEGIN;

CREATE TABLE IF NOT EXISTS preparation_generation_jobs (
    id                VARCHAR(64)  PRIMARY KEY,
    user_sub          VARCHAR(255) NOT NULL,
    phase             VARCHAR(40)  NOT NULL DEFAULT 'queued',
    current_doc       TEXT,
    docs_processed    INTEGER      NOT NULL DEFAULT 0,
    docs_total        INTEGER      NOT NULL DEFAULT 0,
    preparation_id    VARCHAR(64),
    error             TEXT,
    started_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_prep_gen_jobs_user_sub
    ON preparation_generation_jobs (user_sub);

CREATE INDEX IF NOT EXISTS idx_prep_gen_jobs_finished_at
    ON preparation_generation_jobs (finished_at)
    WHERE finished_at IS NOT NULL;

COMMIT;
