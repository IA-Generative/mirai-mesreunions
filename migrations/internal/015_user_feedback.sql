-- Migration 015 — Table user_feedback (boucle d'amélioration utilisateur)
--
-- Centralise 2 types de feedback laissés par l'utilisateur depuis la fiche
-- détaillée d'une réunion :
--
--   * type='usefulness'  — pouce ↑/↓ + checklist raisons + free-text optionnel
--                          en réponse à "Cette retranscription a-t-elle été
--                          utile ?". Le payload jsonb contient :
--                            { thumb: 'up'|'down',
--                              reasons: ['transcription_imprecise', ...],
--                              free_text: '<message libre>' }
--
--   * type='regenerate'  — demande de relancer un pipeline. Payload :
--                            { scope: 'full' | 'llm-only',
--                              reason: '<pourquoi (champ libre)>' }
--                          scope='full' = re-Whisper + re-pyannote + tout
--                          scope='llm-only' = juste glossary → CR (reprocess
--                          existant via /api/meetings/<id>/reprocess)
--
-- Tous les feedbacks sont visibles dans le tab admin (export CSV) + dans
-- la vue 'Mes feedbacks' côté utilisateur avec un tag "pris en compte"
-- piloté par status='processed'.
--
-- Pas d'anonymisation pour la phase 1 (single-user prod-bêta) — à ajouter
-- quand on ouvrira l'app à d'autres utilisateurs.
--
-- Idempotente : CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS user_feedback (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_sub          text NOT NULL,
    -- file_id pointe vers user_audio_files.id mais on NE met pas de FK
    -- (les fichiers peuvent être supprimés / trashed, les feedbacks
    -- doivent rester pour analyse) — nullable pour un feedback global.
    file_id           uuid,
    -- 'usefulness' | 'regenerate'
    type              varchar(32) NOT NULL,
    -- Payload structuré (cf docstring du commit pour le schéma par type).
    payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Cycle de vie : 'new' (juste créé), 'processed' (admin a pris en
    -- compte), 'dismissed' (admin a écarté, hors-scope ou erreur user).
    status            varchar(16) NOT NULL DEFAULT 'new',
    -- Suggestion LLM générée par un cron post-feedback (champ rempli
    -- ultérieurement par un job batch, vide à la création).
    ai_suggestion     text,
    -- Marqueurs admin de prise en compte.
    processed_at      timestamp with time zone,
    processed_by      text,
    admin_comment     text,
    created_at        timestamp with time zone NOT NULL DEFAULT now()
);

-- Requête "mes feedbacks" : tous les feedbacks d'un user_sub trié par date.
CREATE INDEX IF NOT EXISTS ix_user_feedback_user_created
    ON user_feedback (user_sub, created_at DESC);

-- Requête admin "nouveaux à traiter" : status='new' trié par date asc.
CREATE INDEX IF NOT EXISTS ix_user_feedback_status_created
    ON user_feedback (status, created_at)
    WHERE status = 'new';

-- Requête par fichier : voir les feedbacks d'une réunion donnée.
CREATE INDEX IF NOT EXISTS ix_user_feedback_file
    ON user_feedback (file_id)
    WHERE file_id IS NOT NULL;
