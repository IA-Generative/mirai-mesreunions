-- 010_meeting_briefs.sql
--
-- Persiste les briefs de pré-réunion produits par /api/meeting-prep en
-- objets de première classe (avant cette migration ils étaient générés
-- puis jetés). Aligne le brief sur la grammaire mydevices :
--
--   * isolation par user_sub (OIDC sub)
--   * soft-delete via trashed_at (NULL = visible, NOT NULL = corbeille)
--   * purge auto 30j déclenchée par code-generator (TRASH_RETENTION_DAYS)
--
-- Vit en zone INTERNE (postgres-internal) comme user_audio_files. L'écriture
-- depuis code-generator (zone externe) passe par token-issuer
-- /api/v1/briefs/* (cf. delete_file_by_session / rename_file_by_session
-- pour le pattern de relais cross-cluster).

CREATE TABLE IF NOT EXISTS meeting_briefs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_sub text NOT NULL,
  subject text,
  drive_folder_id text,
  role text,
  expectation text,
  focus jsonb,
  duration_minutes int,
  brief_json jsonb,
  documents jsonb,
  title text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz,
  trashed_at timestamptz
);

-- Listing actif (par user_sub, briefs non trashed, ordre récent → ancien).
CREATE INDEX IF NOT EXISTS ix_meeting_briefs_user_active
  ON meeting_briefs(user_sub) WHERE trashed_at IS NULL;

-- Balayage de purge (briefs en corbeille, par date d'envoi à la corbeille).
CREATE INDEX IF NOT EXISTS ix_meeting_briefs_trashed
  ON meeting_briefs(trashed_at) WHERE trashed_at IS NOT NULL;
