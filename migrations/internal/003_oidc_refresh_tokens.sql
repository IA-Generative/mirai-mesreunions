-- ============================================================================
-- Migration 003 : table `oidc_refresh_tokens` pour le push MCR asynchrone
--
-- Pourquoi ?
--   La transcription bascule du stub local vers une plateforme externe MCR
--   appelée via l'API gateway. Cet appel doit s'authentifier via un access
--   token Keycloak *au nom de l'utilisateur* qui a uploadé le fichier — donc
--   bien après que sa session web ait expiré. On capture le `refresh_token`
--   au login (avec scope `offline_access`), on le chiffre côté serveur
--   (Fernet) et on le persiste ici, indexé par user_sub.
--
-- Colonnes :
--   user_sub        OIDC subject (clé primaire — un seul refresh actif par user)
--   ciphertext      Fernet ciphertext du refresh_token (text base64 URL-safe)
--   keycloak_iss    issuer au moment de la capture (multi-realm safety)
--   user_email      copie pratique pour ops/debug
--   last_login_at   moment de la dernière capture/UPSERT
--   created_at, updated_at
--
-- Cible : postgres-internal (database `audio_upload_int`).
-- Idempotent : ré-exécutable.
-- ============================================================================

\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE IF NOT EXISTS oidc_refresh_tokens (
    user_sub        VARCHAR(255) PRIMARY KEY,
    ciphertext      TEXT NOT NULL,
    keycloak_iss    VARCHAR(512),
    user_email      VARCHAR(255),
    last_login_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_oidc_refresh_tokens_last_login
    ON oidc_refresh_tokens (last_login_at);

-- Add the MCR meeting id column on user_audio_files so we can cross-reference
-- with the MCR platform when investigating outcomes. Idempotent.
ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS mcr_meeting_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_user_audio_mcr_meeting_id
    ON user_audio_files (mcr_meeting_id);

COMMIT;

-- Vérification
SELECT
  count(*) AS oidc_rows,
  count(DISTINCT keycloak_iss) AS distinct_issuers
FROM oidc_refresh_tokens;
