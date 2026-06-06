-- Durcissement de la vérification libre du code court (QR) : anti-bruteforce
-- et anti-partage.
--
--   * failed_attempts / locked_at : verrou par-code autoritaire (robuste
--     multi-réplicas). Compteur de tentatives échouées sur la session ;
--     au-delà d'un seuil, la session est gelée et toute tentative reçoit une
--     réponse d'erreur uniforme (pas d'oracle invalide/expiré/épuisé).
--   * claimed_by_device_id / claimed_at : liaison mono-device. Au premier
--     usage réussi, le code est lié à l'appareil enrôlé (device_id, pas l'IP
--     → proxy-safe) ; un second appareil sur le même code est refusé.
--
-- Additif et idempotent : "ADD COLUMN IF NOT EXISTS" (PostgreSQL 9.6+).

ALTER TABLE upload_sessions
    ADD COLUMN IF NOT EXISTS failed_attempts INTEGER NOT NULL DEFAULT 0;

ALTER TABLE upload_sessions
    ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ;

ALTER TABLE upload_sessions
    ADD COLUMN IF NOT EXISTS claimed_by_device_id VARCHAR(255);

ALTER TABLE upload_sessions
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
