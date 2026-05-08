-- ============================================================================
-- Migration 002 : DeviceEnrollment — état `pending`, fusion browser↔PWA, purge
--
-- Pourquoi ?
--   On introduit un état `pending` pour les enrôlements qui n'ont pas encore
--   reçu leur premier heartbeat (= preuve que le client a bien stocké son
--   device_token). On élimine ainsi les enrôlements morts qui polluaient
--   `mydevices`. On ajoute aussi `fp_hash` pour fusionner le rebond
--   browser→PWA installée (même appareil physique, deux storages distincts).
--
-- Colonnes ajoutées :
--   confirmed_at  TIMESTAMPTZ  — set au 1er heartbeat ou 1er upload réussi
--   purge_at      TIMESTAMPTZ  — auto-purge des `pending` non confirmés
--   fp_hash       VARCHAR(64)  — hash normalisé pour la fusion 15 min
--
-- Backfill :
--   Tous les enrôlements existants sont considérés confirmés
--   (confirmed_at = created_at) afin de ne pas masquer les appareils légitimes.
--
-- Cible : postgres-internal (database `audio_upload_int`).
-- Idempotent : ré-exécutable sans dommage.
-- ============================================================================

\set ON_ERROR_STOP on

BEGIN;

-- 1. Nouvelles colonnes (idempotent grâce à IF NOT EXISTS)
ALTER TABLE device_enrollments
  ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS purge_at     TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS fp_hash      VARCHAR(64) NULL;

-- 2. Backfill : tous les rows existants sont déjà "active" → on les confirme
UPDATE device_enrollments
   SET confirmed_at = created_at
 WHERE confirmed_at IS NULL
   AND status = 'active';

-- 3. Index pour la fusion (qr_token, fp_hash) et la purge (status, purge_at)
CREATE INDEX IF NOT EXISTS ix_device_qr_fphash
  ON device_enrollments (qr_token, fp_hash);

CREATE INDEX IF NOT EXISTS ix_device_status_purge
  ON device_enrollments (status, purge_at);

CREATE INDEX IF NOT EXISTS ix_device_fp_hash
  ON device_enrollments (fp_hash);

COMMIT;

-- 4. Vérification
SELECT
  count(*)                                                       AS total,
  count(*) FILTER (WHERE confirmed_at IS NOT NULL)               AS confirmed,
  count(*) FILTER (WHERE status = 'pending')                     AS pending_rows,
  count(*) FILTER (WHERE status = 'active')                      AS active_rows
FROM device_enrollments;
