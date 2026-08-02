-- 023 — Retry différé des jobs video-ingest (incident anti-bot 2026-08-02).
--
-- Contexte : YouTube sert « Sign in to confirm you're not a bot » par
-- intermittence à l'IP d'egress du cluster (NAT SCW mutualisée). La même
-- vidéo échouait à 09:59 et passait à 10:03. Le worker classait pourtant
-- l'échec comme terminal (`status='failed'`, jamais reclaimé), donc
-- l'import était définitivement perdu pour l'utilisateur.
--
-- `attempts` existait déjà (incrémenté au claim) mais n'était jamais relu.
-- On ajoute la seule pièce manquante : une date de réarmement, pour que
-- `claim_next` puisse remettre un job en file SANS le reprendre en boucle
-- serrée (le worker poll toutes les 5s).
--
-- Idempotent : rejouable sans effet de bord.
BEGIN;

ALTER TABLE video_ingest_jobs
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;

COMMENT ON COLUMN video_ingest_jobs.next_attempt_at IS
    'Réarmement du backoff : un job pending n''est claimable qu''à partir '
    'de cette date. NULL = claimable immédiatement (cas nominal).';

-- Le hot path `claim_next` filtre désormais sur next_attempt_at. L'index
-- de dispatch existant (status, lease_until, created_at) reste valable ;
-- on en ajoute un dédié aux jobs en attente de réarmement pour éviter que
-- le backoff ne dégrade le scan quand la file grossit.
CREATE INDEX IF NOT EXISTS ix_video_ingest_jobs_next_attempt
    ON video_ingest_jobs (next_attempt_at, created_at)
    WHERE status = 'pending' AND next_attempt_at IS NOT NULL;

COMMIT;
