-- 011_brief_audio_link_and_series.sql
--
-- Meeting-prep v2 (cf plan ok-on-continue-sur-eager-hickey.md) :
--
--   * lien brief ↔ audio (auto-link déterministe AVANT transcription)
--   * tracking re-traitement (glossary_correction relancé avec glossaire amendé)
--   * chaînage série (series_parent_id)
--   * glossaire utilisateur global (table dédiée, cap 300 termes/user)
--   * versement Drive best-effort (drive_prep_folder_id + sync status)
--   * date prévue de la réunion (target_meeting_date)
--
-- Vit en zone INTERNE (postgres-internal). Application :
--   kubectl exec -i statefulset/postgres-internal -- psql … < 011_*.sql
-- (cf. feedback_migration_before_rollout : avant tout rollout du service)

-- ─── user_audio_files : lien brief + tracking re-traitement ──────

ALTER TABLE user_audio_files
  ADD COLUMN IF NOT EXISTS meeting_brief_id UUID;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_user_audio_meeting_brief'
  ) THEN
    ALTER TABLE user_audio_files
      ADD CONSTRAINT fk_user_audio_meeting_brief
      FOREIGN KEY (meeting_brief_id) REFERENCES meeting_briefs(id) ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_uaf_meeting_brief
  ON user_audio_files(meeting_brief_id) WHERE meeting_brief_id IS NOT NULL;

ALTER TABLE user_audio_files
  ADD COLUMN IF NOT EXISTS reprocess_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user_audio_files
  ADD COLUMN IF NOT EXISTS reprocessed_with_brief_id UUID;
ALTER TABLE user_audio_files
  ADD COLUMN IF NOT EXISTS last_reprocessed_at TIMESTAMPTZ;
ALTER TABLE user_audio_files
  ADD COLUMN IF NOT EXISTS reprocess_history JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE user_audio_files
  ADD COLUMN IF NOT EXISTS suggested_brief_dismissed_id UUID;

-- ─── meeting_briefs : série + engagement + drive sync ────────────

ALTER TABLE meeting_briefs
  ADD COLUMN IF NOT EXISTS last_viewed_at TIMESTAMPTZ;

ALTER TABLE meeting_briefs
  ADD COLUMN IF NOT EXISTS series_parent_id UUID;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_meeting_brief_series_parent'
  ) THEN
    ALTER TABLE meeting_briefs
      ADD CONSTRAINT fk_meeting_brief_series_parent
      FOREIGN KEY (series_parent_id) REFERENCES meeting_briefs(id) ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_mb_series_parent
  ON meeting_briefs(series_parent_id) WHERE series_parent_id IS NOT NULL;

-- Date prévue de la réunion (saisie wizard, optionnelle, default J+1).
ALTER TABLE meeting_briefs
  ADD COLUMN IF NOT EXISTS target_meeting_date DATE;

-- Versement Drive best-effort (cf §9bis du plan).
ALTER TABLE meeting_briefs
  ADD COLUMN IF NOT EXISTS drive_prep_folder_id TEXT;
ALTER TABLE meeting_briefs
  ADD COLUMN IF NOT EXISTS drive_prep_root_folder_id TEXT;
ALTER TABLE meeting_briefs
  ADD COLUMN IF NOT EXISTS drive_sync_status TEXT;  -- 'pending'|'synced'|'failed'
ALTER TABLE meeting_briefs
  ADD COLUMN IF NOT EXISTS drive_synced_at TIMESTAMPTZ;

-- ─── user_glossary_terms : glossaire utilisateur global ──────────

CREATE TABLE IF NOT EXISTS user_glossary_terms (
  user_sub TEXT NOT NULL,
  term TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  last_source_brief_id UUID,
  curated_by_user BOOLEAN NOT NULL DEFAULT FALSE,
  blacklisted BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (user_sub, term)
);

CREATE INDEX IF NOT EXISTS ix_ugt_user_active
  ON user_glossary_terms(user_sub, last_seen_at DESC)
  WHERE NOT blacklisted;
