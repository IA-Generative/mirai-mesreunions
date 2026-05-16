-- 012_split_briefs.sql
--
-- Split du modèle DB meeting-prep : `meeting_briefs` est éclatée en deux
-- tables de première classe :
--
--   * `preparations` — entité amont-réunion (titre, contexte, participants,
--     contenu structuré, glossaire-source, sync Drive). Reprend les colonnes
--     utiles de `meeting_briefs` côté préparation.
--   * `meetings`     — entité post-réunion (CR, summary, contenu structuré).
--     Liée optionnellement à un audio (`user_audio_files.id`) ET/OU à une
--     préparation (`preparations.id`). Cardinalité 0..1 ↔ 0..1.
--
-- Décisions inscrites par l'utilisateur (cf prompt-refacto-mydevices-nocturne-v2.md) :
--   * B5 : meetings standalone + FK NULLABLE vers user_audio_files. Une
--          réunion peut exister sans audio (CR manuel, réunion non enregistrée).
--   * B6 : DROP confirmé — perte des briefs prod-bêta acceptée explicitement.
--          Pas de migration data, pas d'archive, pas de dual-write.
--   * B2 : pattern existant migrations/internal/NNN_*.sql, pas d'Alembic.
--
-- Vit en zone INTERNE (postgres-internal) comme `meeting_briefs`, owner réel
-- = token-issuer. Application :
--   kubectl exec -i statefulset/postgres-internal -- psql … < 012_*.sql
-- (cf feedback_migration_before_rollout : avant tout rollout du code qui lit
-- les nouvelles tables — sinon SQLAlchemy retourne UndefinedColumn en boucle).
--
-- Ordre obligatoire :
--   1) DROP `meeting_briefs` (et la FK + index dépendants sur user_audio_files)
--   2) CREATE `preparations`
--   3) CREATE `meetings`
--   4) Re-câbler `user_audio_files.meeting_brief_id` → conservé renommé
--      `meeting_id` (FK vers meetings) — le pipeline file-mover créera désormais
--      une row `meetings` à l'upload audio et y branchera l'audio (cf PR2d).

-- ─── 1) Démantèlement meeting_briefs ───────────────────────────

-- Drop la FK `user_audio_files → meeting_briefs` puis l'index associé puis
-- la colonne. Idempotent (IF EXISTS) pour rejouabilité.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_user_audio_meeting_brief'
  ) THEN
    ALTER TABLE user_audio_files DROP CONSTRAINT fk_user_audio_meeting_brief;
  END IF;
END $$;

DROP INDEX IF EXISTS ix_uaf_meeting_brief;

-- On conserve les colonnes de tracking re-traitement (reprocess_*) introduites
-- par 011 — elles restent utiles pour le pipeline meetings. Seules les
-- colonnes spécifiquement liées au brief amont sont renommées/recâblées :
--   meeting_brief_id              → meeting_id           (FK → meetings.id)
--   reprocessed_with_brief_id     → reprocessed_with_meeting_id
--   suggested_brief_dismissed_id  → suggested_meeting_dismissed_id
--
-- Note : la donnée actuellement présente dans ces colonnes pointe vers
-- `meeting_briefs.id` (qui va être droppée). Comme B6 acte la perte, on
-- NULLifie d'abord pour éviter une FK fantôme.

UPDATE user_audio_files
   SET meeting_brief_id = NULL,
       reprocessed_with_brief_id = NULL,
       suggested_brief_dismissed_id = NULL
 WHERE meeting_brief_id IS NOT NULL
    OR reprocessed_with_brief_id IS NOT NULL
    OR suggested_brief_dismissed_id IS NOT NULL;

ALTER TABLE user_audio_files
  RENAME COLUMN meeting_brief_id TO meeting_id;
ALTER TABLE user_audio_files
  RENAME COLUMN reprocessed_with_brief_id TO reprocessed_with_meeting_id;
ALTER TABLE user_audio_files
  RENAME COLUMN suggested_brief_dismissed_id TO suggested_meeting_dismissed_id;

-- DROP la table. Cascade pour balayer toute référence résiduelle (index
-- partiels, séquences, etc.).
DROP TABLE IF EXISTS meeting_briefs CASCADE;

-- ─── 2) preparations (amont-réunion) ────────────────────────────

CREATE TABLE preparations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_sub text NOT NULL,

  -- Contenu utilisateur (saisie wizard).
  title text,
  subject text,
  role text,
  expectation text,
  focus jsonb,
  duration_minutes int,
  participants jsonb,
  context text,
  target_meeting_date date,

  -- Sortie LLM (brief structuré) + métadonnées docs ingérés.
  content jsonb,                 -- ex `brief_json` de l'ancien modèle
  documents jsonb,
  glossary_source jsonb,         -- termes glossaire extraits, source du user_glossary_terms

  -- Chaînage série (préparation parent dans une chaîne de réunions).
  series_parent_id uuid,

  -- Sync Drive best-effort (cf §9bis du plan v1).
  drive_folder_id text,          -- folder de sortie choisi par l'utilisateur
  drive_prep_folder_id text,     -- sous-folder créé pour cette prep
  drive_prep_root_folder_id text,
  drive_sync_status text,        -- 'pending' | 'synced' | 'failed'
  drive_synced_at timestamptz,

  -- Engagement (signal d'auto-link avec un audio).
  last_viewed_at timestamptz,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz,
  trashed_at timestamptz,

  CONSTRAINT fk_preparation_series_parent
    FOREIGN KEY (series_parent_id) REFERENCES preparations(id) ON DELETE SET NULL
);

CREATE INDEX ix_preparations_user_active
  ON preparations(user_sub) WHERE trashed_at IS NULL;
CREATE INDEX ix_preparations_trashed
  ON preparations(trashed_at) WHERE trashed_at IS NOT NULL;
CREATE INDEX ix_preparations_created
  ON preparations(created_at DESC) WHERE trashed_at IS NULL;
CREATE INDEX ix_preparations_series_parent
  ON preparations(series_parent_id) WHERE series_parent_id IS NOT NULL;

-- ─── 3) meetings (post-réunion) ─────────────────────────────────

CREATE TABLE meetings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_sub text NOT NULL,

  -- Identité côté utilisateur.
  title text,
  summary text,                  -- résumé court (key_points)
  content jsonb,                 -- compte-rendu structuré complet

  -- Lien optionnel vers l'audio source (NULL si CR manuel, réunion non
  -- enregistrée). ON DELETE SET NULL : suppression définitive d'un audio
  -- ne purge pas le CR.
  user_audio_file_id uuid,
  -- Lien optionnel vers la préparation amont. NULL si pas de prep.
  -- ON DELETE SET NULL : drop d'une prep ne purge pas le meeting.
  preparation_id uuid,

  -- Sync Drive best-effort (mêmes invariants que preparations).
  drive_folder_id text,
  drive_sync_status text,        -- 'pending' | 'synced' | 'failed'
  drive_synced_at timestamptz,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz,
  trashed_at timestamptz,

  CONSTRAINT fk_meeting_audio
    FOREIGN KEY (user_audio_file_id) REFERENCES user_audio_files(id) ON DELETE SET NULL,
  CONSTRAINT fk_meeting_preparation
    FOREIGN KEY (preparation_id) REFERENCES preparations(id) ON DELETE SET NULL
);

CREATE INDEX ix_meetings_user_active
  ON meetings(user_sub) WHERE trashed_at IS NULL;
CREATE INDEX ix_meetings_trashed
  ON meetings(trashed_at) WHERE trashed_at IS NOT NULL;
CREATE INDEX ix_meetings_created
  ON meetings(created_at DESC) WHERE trashed_at IS NULL;
CREATE INDEX ix_meetings_preparation
  ON meetings(preparation_id) WHERE preparation_id IS NOT NULL;
CREATE INDEX ix_meetings_audio
  ON meetings(user_audio_file_id) WHERE user_audio_file_id IS NOT NULL;

-- ─── 4) Re-câblage user_audio_files.meeting_id → meetings.id ────

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_user_audio_meeting'
  ) THEN
    ALTER TABLE user_audio_files
      ADD CONSTRAINT fk_user_audio_meeting
      FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_uaf_meeting
  ON user_audio_files(meeting_id) WHERE meeting_id IS NOT NULL;

-- ─── 5) Glossaire utilisateur — re-typer last_source_brief_id ──

-- Le glossaire utilisateur (table user_glossary_terms, migration 011) référence
-- last_source_brief_id pointant vers meeting_briefs(id). Comme la table est
-- droppée, on renomme la colonne et on NULLifie les valeurs (équivalent à
-- "perdre le tracking de l'origine" — acceptable, le glossaire utilisateur
-- reste valable, seule la traçabilité par brief disparaît).

UPDATE user_glossary_terms SET last_source_brief_id = NULL
 WHERE last_source_brief_id IS NOT NULL;
ALTER TABLE user_glossary_terms
  RENAME COLUMN last_source_brief_id TO last_source_meeting_id;
