-- 001 (external schema) — Index composite pour /api/my-sessions.
--
-- La liste réunions (mesreunions-web) tape :
--   SELECT * FROM upload_sessions
--    WHERE user_sub = $1 AND trashed_at IS NULL
--    ORDER BY created_at DESC LIMIT 20;
--
-- Sans cet index, le plan retombait sur ix_session_expires (faible
-- sélectivité) ou un seq-scan dès que la table dépasse quelques
-- milliers de lignes — cause principale de la lenteur perçue.
--
-- CREATE INDEX CONCURRENTLY pour éviter de verrouiller la table en
-- prod ; à exécuter HORS transaction (postgres exige NOT IN TX pour
-- CONCURRENTLY).

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_session_user_active_created
    ON upload_sessions (user_sub, trashed_at, created_at DESC);
