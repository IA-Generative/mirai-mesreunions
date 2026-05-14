-- ============================================================================
-- Migration 009 : résumé pour les absents (Kevent meeting-intelligence)
--
-- Pourquoi ?
--   Le pipeline produit déjà la transcription, la reformulation et l'analyse
--   5-sections, mais ces livrables sont pensés pour quelqu'un qui était dans
--   la réunion. Pour les personnes qui n'ont pas pu y assister, l'animateur
--   doit aujourd'hui composer manuellement un débrief — coûteux en temps et
--   inégal selon les conducteurs.
--
--   On ajoute une dernière étape LLM (medium model) qui produit un résumé
--   autoportant de 150-300 mots écrit pour les absents : objet de la réunion,
--   3-5 points-clés, actions/engagements, points laissés en suspens. Stocké
--   dans une colonne dédiée pour préserver les outputs précédents et garder
--   l'audit "qu'a produit chaque étape ?".
--
-- Toggle : KEVENT_ABSENTEE_SUMMARY_ENABLED (env var). NULL si désactivé OU
--   si l'appel LLM a échoué (le statut bascule alors en kevent_partially_completed).
--
-- Cible : postgres-internal (database `audio_upload_int`).
-- Idempotent.
-- ============================================================================

\set ON_ERROR_STOP on

BEGIN;

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS absentee_summary TEXT;

COMMIT;

SELECT
  count(*)                                              AS total,
  count(*) FILTER (WHERE absentee_summary IS NOT NULL)  AS with_absentee_summary
FROM user_audio_files;
