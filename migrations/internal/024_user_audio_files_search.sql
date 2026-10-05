-- 024 — Recherche plein texte sur les réunions (contrat de recherche MirAI)
--
-- Sert `POST /api/v1/audio/search` (internal-ingester), appelé par
-- `GET /api/v1/search` de mesreunions-web pour Mon portail. Recherche
-- seulement : aucun appel à un modèle de langage derrière.
--
-- Document indexé, pondéré :
--   A  titre        = suggested_filename, à défaut original_filename
--   B  points clés  = key_points_summary
--   C  transcription = speaker_tagged_text, à défaut cleaned_text, à défaut
--                      transcription_text (même ordre que rag-export : l'index
--                      correspond au texte dont sont tirés les extraits).
--                      Les en-têtes de blocs `**Nom** _(m:ss → m:ss)_` sont
--                      retirés : sinon « intervenant » trouverait toutes les
--                      réunions. Bornée à 400 000 caractères (≈ 6 h de parole)
--                      pour rester sous la limite de taille d'un tsvector.
--
-- Le document est STOCKÉ dans `search_tsv`, tenu à jour par trigger :
--   - pas de colonne GENERATED STORED : l'ajouter réécrirait la table sous
--     verrou exclusif, et elle serait recalculée à CHAQUE UPDATE, y compris le
--     battement de cœur du watchdog toutes les 30 s ;
--   - le trigger ne se déclenche que sur les colonnes de texte, et ne recalcule
--     que si l'une d'elles a réellement changé ;
--   - la recherche classe (ts_rank) sur la colonne, sans recalculer le
--     document de chaque réunion trouvée.
--
-- Accents : configuration `french_unaccent` (copie de `french`, plus
-- `unaccent` si l'extension est disponible), utilisée partout. Sans
-- l'extension (droits ou paquet absents), la configuration reste du `french`
-- simple, la migration l'annonce par le NOTICE « unaccent absent : recherche
-- sensible aux accents », et « reunion » ne trouve pas « réunion ».
-- Vérifier AVANT d'appliquer, sur postgres-internal :
--   SELECT name, installed_version FROM pg_available_extensions WHERE name = 'unaccent';
--
-- Installer unaccent APRÈS coup ne suffit pas. Procédure complète :
--   1. CREATE EXTENSION IF NOT EXISTS unaccent;          (droit requis)
--   2. ALTER TEXT SEARCH CONFIGURATION french_unaccent
--        ALTER MAPPING FOR hword, hword_part, word WITH unaccent, french_stem;
--      (ou rejouer ce fichier, qui le fait)
--   3. RECALCULER TOUTE la colonne par lots — un REINDEX ne suffit pas : les
--      vecteurs stockés ont été calculés avec l'ancienne configuration.
--      Entre 2 et la fin de 3, une requête sur un terme accentué ne trouve
--      plus les lignes pas encore recalculées : à faire hors des heures
--      d'usage.
--        DO $$
--        DECLARE last_id uuid := '00000000-0000-0000-0000-000000000000'; n int;
--        BEGIN
--          LOOP
--            WITH b AS (SELECT id FROM user_audio_files
--                        WHERE id > last_id ORDER BY id LIMIT 200)
--            UPDATE user_audio_files u
--               SET search_tsv = uaf_search_tsv(
--                       coalesce(u.suggested_filename, u.original_filename),
--                       u.key_points_summary,
--                       coalesce(u.speaker_tagged_text, u.cleaned_text, u.transcription_text))
--              FROM b WHERE u.id = b.id;
--            GET DIAGNOSTICS n = ROW_COUNT;
--            EXIT WHEN n = 0;
--            SELECT id INTO last_id FROM user_audio_files
--             WHERE id > last_id ORDER BY id OFFSET n - 1 LIMIT 1;
--            COMMIT;
--          END LOOP;
--        END $$;
--
-- Idempotente : rejouable sans effet. Pas de BEGIN/COMMIT global :
--   - le rattrapage valide par lots (COMMIT dans le bloc DO) pour ne pas
--     garder de verrou long sur la table ;
--   - l'index est construit CONCURRENTLY, interdit dans une transaction.
-- Si une construction d'index est interrompue, Postgres laisse un index
-- INVALID que IF NOT EXISTS ne reconstruit pas : le supprimer
-- (`DROP INDEX CONCURRENTLY ix_uaf_search_tsv;`) puis rejouer ce fichier.
--
-- À appliquer AVANT le déploiement du code de recherche : sans la colonne,
-- la route répond 503 search_unavailable. Exécuter avec psql SANS -1.

-- ─── 1. Configuration plein texte ──────────────────────────────────────────

DO $$
BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS unaccent;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'unaccent absent : recherche sensible aux accents (%)', SQLERRM;
    END;

    IF NOT EXISTS (SELECT 1 FROM pg_ts_config
                    WHERE cfgname = 'french_unaccent' AND pg_ts_config_is_visible(oid)) THEN
        CREATE TEXT SEARCH CONFIGURATION french_unaccent (COPY = french);
    END IF;

    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'unaccent') THEN
        ALTER TEXT SEARCH CONFIGURATION french_unaccent
            ALTER MAPPING FOR hword, hword_part, word WITH unaccent, french_stem;
    ELSE
        RAISE NOTICE 'unaccent absent : recherche sensible aux accents (french_unaccent = french simple)';
    END IF;
END $$;

-- ─── 2. Document et colonne stockée ────────────────────────────────────────

CREATE OR REPLACE FUNCTION uaf_search_tsv(p_title text, p_key_points text, p_transcript text)
RETURNS tsvector
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT setweight(to_tsvector('french_unaccent'::regconfig, coalesce(p_title, '')), 'A')
      || setweight(to_tsvector('french_unaccent'::regconfig, coalesce(p_key_points, '')), 'B')
      || setweight(to_tsvector('french_unaccent'::regconfig,
             left(regexp_replace(coalesce(p_transcript, ''),
                                 '^[ \t]*\*\*[^*]+\*\*[ \t]*_\([^)]*\)_[ \t]*$', ' ', 'gn'),
                  400000)), 'C')
$$;

COMMENT ON FUNCTION uaf_search_tsv(text, text, text) IS
    'Document plein texte d''une réunion (titre A, points clés B, transcription C sans '
    'en-têtes de blocs). Alimente user_audio_files.search_tsv via trg_uaf_search_tsv_*.';

ALTER TABLE user_audio_files ADD COLUMN IF NOT EXISTS search_tsv tsvector;

COMMENT ON COLUMN user_audio_files.search_tsv IS
    'Document plein texte (migration 024), tenu à jour par trigger. NULL = pas encore calculé.';

CREATE OR REPLACE FUNCTION uaf_search_tsv_refresh()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.search_tsv := uaf_search_tsv(
        coalesce(NEW.suggested_filename, NEW.original_filename),
        NEW.key_points_summary,
        coalesce(NEW.speaker_tagged_text, NEW.cleaned_text, NEW.transcription_text));
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_uaf_search_tsv_insert ON user_audio_files;
CREATE TRIGGER trg_uaf_search_tsv_insert
    BEFORE INSERT ON user_audio_files
    FOR EACH ROW EXECUTE FUNCTION uaf_search_tsv_refresh();

DROP TRIGGER IF EXISTS trg_uaf_search_tsv_update ON user_audio_files;
CREATE TRIGGER trg_uaf_search_tsv_update
    BEFORE UPDATE OF suggested_filename, original_filename, key_points_summary,
                     speaker_tagged_text, cleaned_text, transcription_text
    ON user_audio_files
    FOR EACH ROW
    WHEN (OLD.suggested_filename IS DISTINCT FROM NEW.suggested_filename
       OR OLD.original_filename IS DISTINCT FROM NEW.original_filename
       OR OLD.key_points_summary IS DISTINCT FROM NEW.key_points_summary
       OR OLD.speaker_tagged_text IS DISTINCT FROM NEW.speaker_tagged_text
       OR OLD.cleaned_text IS DISTINCT FROM NEW.cleaned_text
       OR OLD.transcription_text IS DISTINCT FROM NEW.transcription_text)
    EXECUTE FUNCTION uaf_search_tsv_refresh();

-- ─── 3. Rattrapage par lots ────────────────────────────────────────────────
-- Le trigger couvre déjà les écritures concurrentes ; on ne calcule que les
-- lignes encore à NULL, 200 par transaction.

DO $$
DECLARE
    n integer;
BEGIN
    LOOP
        UPDATE user_audio_files u
           SET search_tsv = uaf_search_tsv(
                   coalesce(u.suggested_filename, u.original_filename),
                   u.key_points_summary,
                   coalesce(u.speaker_tagged_text, u.cleaned_text, u.transcription_text))
         WHERE u.id IN (SELECT id FROM user_audio_files
                         WHERE search_tsv IS NULL
                         LIMIT 200);
        GET DIAGNOSTICS n = ROW_COUNT;
        EXIT WHEN n = 0;
        COMMIT;
    END LOOP;
END $$;

-- ─── 4. Index ──────────────────────────────────────────────────────────────

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_uaf_search_tsv
    ON user_audio_files USING GIN (search_tsv);
