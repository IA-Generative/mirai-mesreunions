-- Soft-delete (corbeille) pour les sessions et fichiers uploadés.
--
-- L'utilisateur peut "mettre à la corbeille" un fichier ou une session
-- depuis mydevices : on positionne `trashed_at = now()` au lieu de
-- détruire la row + les objets S3. Les éléments restent restaurables
-- pendant 30 jours, puis sont définitivement supprimés (S3 + DB) par
-- le balayage opportuniste exécuté par code-generator au début de
-- chaque GET /api/my-sessions (pas besoin de cron dédié).
--
-- Idempotent : "ADD COLUMN IF NOT EXISTS" (PostgreSQL 9.6+).

ALTER TABLE upload_sessions
    ADD COLUMN IF NOT EXISTS trashed_at TIMESTAMPTZ;

ALTER TABLE uploaded_files
    ADD COLUMN IF NOT EXISTS trashed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_session_trashed_at  ON upload_sessions (trashed_at) WHERE trashed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_file_trashed_at     ON uploaded_files  (trashed_at) WHERE trashed_at IS NOT NULL;
