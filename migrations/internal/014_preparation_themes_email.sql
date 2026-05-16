-- Migration 014 — Thématiques personnalisables (Lot 9) + Emails CR (Lot 8)
--
-- Ajoute 3 colonnes à `preparations` :
--   * themes                     : liste libre de thématiques utilisateur
--                                  (capée 50 côté backend), distincte du
--                                  champ `focus` (checkboxes pré-définies).
--                                  Structure : JSONB array of strings.
--                                  Ex : ["Budget", "Stratégie 2026"]
--   * send_cr_email              : toggle envoi auto du CR aux participants
--                                  post-transcription (consommé par le hook
--                                  côté pipeline ingester).
--   * drive_main_courante_doc_id : id du document "Main courante" du Drive
--                                  pour les réunions récurrentes (créé à la
--                                  première occurrence avec CR, puis appendé
--                                  à chaque occurrence suivante).
--
-- Idempotente : ALTER ... IF NOT EXISTS, ré-exécutable sans erreur.

ALTER TABLE preparations ADD COLUMN IF NOT EXISTS themes jsonb DEFAULT '[]'::jsonb;
ALTER TABLE preparations ADD COLUMN IF NOT EXISTS send_cr_email boolean DEFAULT false;
ALTER TABLE preparations ADD COLUMN IF NOT EXISTS drive_main_courante_doc_id text;
