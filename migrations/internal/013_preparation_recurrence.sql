-- Migration 013 — Récurrence structurée des préparations (Lot 6)
--
-- Ajoute 3 colonnes à `preparations` pour modéliser des réunions
-- récurrentes (RRULE-like) :
--   * is_recurring        : flag rapide pour filtrage / badge UI
--   * recurrence_rule     : règle structurée JSONB
--       { freq:'WEEKLY'|'MONTHLY'|'DAILY',
--         interval:int,
--         byweekday:['MO','TU',...] (hebdo),
--         byhour:int,
--         byminute:int,
--         until:'YYYY-MM-DD' (optionnel) }
--   * next_occurrence_at  : prochaine date prévue, calculée backend via
--                           python-dateutil rrule (re-calculée à chaque
--                           amend du recurrence_rule ou target_meeting_date)
--
-- Idempotente : ALTER ... IF NOT EXISTS, ré-exécutable sans erreur.

ALTER TABLE preparations ADD COLUMN IF NOT EXISTS is_recurring boolean DEFAULT false;
ALTER TABLE preparations ADD COLUMN IF NOT EXISTS recurrence_rule jsonb;
ALTER TABLE preparations ADD COLUMN IF NOT EXISTS next_occurrence_at timestamptz;

-- Index partiel sur next_occurrence_at limité aux préparations récurrentes :
-- évite un index global gigantesque alors que la grande majorité des
-- préparations seront ponctuelles (is_recurring=false).
CREATE INDEX IF NOT EXISTS ix_preparations_next_occurrence
    ON preparations(next_occurrence_at)
    WHERE is_recurring = true AND trashed_at IS NULL;
