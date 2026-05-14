-- ============================================================================
-- Migration 008 : date/heure de réunion overridée par l'utilisateur
--
-- Pourquoi ?
--   La date d'upload (UploadedFile.created_at, recopiée implicitement par le
--   timestamp de pull dans UserAudioFile) ne correspond pas forcément à la
--   date *réelle* de la réunion enregistrée (différé, batch ancien, etc.).
--   L'utilisateur peut désormais surcharger cette info depuis la fiche
--   détaillée mydevices ; l'absence de valeur = pas d'override, l'UI
--   retombe sur la date d'upload pour l'affichage et le tri.
--
-- Persisté en zone interne (UserAudioFile) car info utilisateur durable
-- — cohérent avec suggested_filename / key_points_summary.
--
-- Écriture : nouvel endpoint token-issuer
--   POST /api/v1/files/by-session/meeting-datetime
-- relayé par code-generator (PATCH /api/file/<id>/meeting-datetime),
-- même pattern que la route rename existante.
--
-- Cible : postgres-internal (database `audio_upload_int`).
-- Idempotent. Migration appliquée AVANT rollout (règle bien connue :
-- sinon SQLAlchemy renvoie UndefinedColumn en boucle).
-- ============================================================================

\set ON_ERROR_STOP on

BEGIN;

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS meeting_datetime TIMESTAMPTZ;

COMMIT;

SELECT
  count(*)                                              AS total,
  count(*) FILTER (WHERE meeting_datetime IS NOT NULL)  AS with_meeting_datetime
FROM user_audio_files;
