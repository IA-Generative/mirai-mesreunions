-- 016 — Renomme les étiquettes locuteur génériques « SPEAKER_NN » en
-- « Intervenant_NN » sur les transcriptions déjà produites.
--
-- Contexte : le diarization_merger émet désormais « Intervenant_NN »
-- (cf. services/dmz-to-internal-bridge/app/diarization_merger.py). Cette
-- migration aligne le contenu historique pour éviter une UI hybride.
--
-- Idempotente : les UPDATE n'affectent que les lignes qui contiennent
-- encore « **SPEAKER_NN** » et le REPLACE est un no-op après la première
-- exécution. Le contenu déjà nommé (vrais noms LLM) n'est pas touché.
--
-- Volume attendu : 5 colonnes TEXT × N lignes (N = taille du parc).
-- Aucun verrou long anticipé sur la cible (postgres : MVCC).
--
-- Le JSON brut diarization_json conserve volontairement les labels
-- « SPEAKER_NN » : il sert de référence interne pour le mapping
-- speaker_names produit par le LLM.

BEGIN;

UPDATE user_audio_files
   SET speaker_tagged_text = REPLACE(speaker_tagged_text, '**SPEAKER_', '**Intervenant_')
 WHERE speaker_tagged_text LIKE '%**SPEAKER\_%' ESCAPE '\';

UPDATE user_audio_files
   SET glossary_corrected_text = REPLACE(glossary_corrected_text, '**SPEAKER_', '**Intervenant_')
 WHERE glossary_corrected_text LIKE '%**SPEAKER\_%' ESCAPE '\';

UPDATE user_audio_files
   SET cleaned_text = REPLACE(cleaned_text, '**SPEAKER_', '**Intervenant_')
 WHERE cleaned_text LIKE '%**SPEAKER\_%' ESCAPE '\';

UPDATE user_audio_files
   SET reformulated_text = REPLACE(reformulated_text, '**SPEAKER_', '**Intervenant_')
 WHERE reformulated_text LIKE '%**SPEAKER\_%' ESCAPE '\';

-- transcription_text est l'output Whisper brut (sans diarisation) — il ne
-- contient normalement pas d'étiquettes SPEAKER_NN, mais on couvre le cas
-- où une étape antérieure aurait injecté des étiquettes par erreur.
UPDATE user_audio_files
   SET transcription_text = REPLACE(transcription_text, '**SPEAKER_', '**Intervenant_')
 WHERE transcription_text LIKE '%**SPEAKER\_%' ESCAPE '\';

COMMIT;
