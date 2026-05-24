-- 021 — Liste de phrases interdites à filtrer automatiquement de toute
-- transcription Whisper. Cf services/dmz-to-internal-bridge/app/
-- forbidden_phrases.py + admin-console pour gestion.
--
-- Motivation : Whisper hallucine régulièrement des phrases parasites issues
-- de son corpus d'entraînement YouTube (sous-titrages, intros vidéo, etc.).
-- Ces scories polluent la transcription brute et se retrouvent dans le CR
-- LLM downstream. Pré-filtrage côté pipeline avant les étapes LLM.
--
-- L'ordre d'évaluation est important : les patterns LONGS doivent être
-- évalués AVANT les courts pour ne pas être masqués. Géré via `ordering`.
--
-- Idempotent : IF NOT EXISTS partout, rejouable sans effet.

BEGIN;

CREATE TABLE IF NOT EXISTS transcription_forbidden_phrases (
    id           SERIAL PRIMARY KEY,
    phrase       TEXT        NOT NULL,
    ordering     INTEGER     NOT NULL DEFAULT 1000,
    is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
    note         TEXT,        -- raison d'ajout, optionnel
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (phrase)
);

CREATE INDEX IF NOT EXISTS ix_forbidden_phrases_active_order
    ON transcription_forbidden_phrases (is_active, ordering);

-- Seed initial (cf TranscriptionForbiddenSentences upstream).
-- Ordre = longueur décroissante pour que les patterns longs matchent en
-- premier (ex : "Sous-titrage Société Radio-Canada" avant
-- "Société Radio-Canada", sinon le second mange le premier).
INSERT INTO transcription_forbidden_phrases (phrase, ordering, note)
VALUES
    ('Le texte dans un langage naturel est un peu plus important pour le texte dans un langage naturel.', 10, 'Hallucination Whisper longue'),
    ('Le texte dans un langage naturel et du texte dans un langage naturel, sans répétition.', 20, 'Hallucination Whisper longue'),
    ('Le texte dans un langage naturel, sans répétition.', 30, 'Hallucination Whisper'),
    ('Merci d''avoir regardé cette vidéo !', 40, 'Outro YouTube fréquente'),
    ('Sous-titrage Société Radio-Canada', 50, 'Crédits sous-titrage TV'),
    ('Société Radio-Canada', 60, 'Crédits sous-titrage TV'),
    ('Sous-titrage FR 2021', 70, 'Crédits sous-titrage TV'),
    ('Sous-titrage FR ?', 80, 'Crédits sous-titrage TV'),
    ('Sous-titrage FR', 90, 'Crédits sous-titrage TV'),
    ('Sous-titrage ST'' 501', 100, 'Crédits sous-titrage TV'),
    ('C''est parti !', 110, 'Intro vidéo fréquente'),
    ('...  ...', 200, 'Ponctuation parasite'),
    ('–', 210, 'Tiret cadratin orphelin')
ON CONFLICT (phrase) DO NOTHING;

COMMIT;
