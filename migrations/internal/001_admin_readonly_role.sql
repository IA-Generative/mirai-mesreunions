-- ============================================================================
-- Migration 001 : rôle Postgres `audio_admin_ro` pour le dashboard admin-portal
--
-- Pourquoi un rôle dédié ?
--   `admin-portal` consomme une K8s Secret distincte (admin-int-db-secret)
--   pour respecter le principe de moindre privilège : le dashboard n'a besoin
--   que de lecture sur la DB interne, pas d'écriture. Si le pod admin-portal
--   est compromis, l'attaquant ne peut pas modifier ni supprimer les données
--   de transcription.
--
-- Cible : postgres-internal (database `audio_upload_int`).
-- Idempotent : peut être ré-exécuté pour rotater le password.
--
-- Variables psql attendues :
--   :admin_password   mot de passe à utiliser pour le rôle audio_admin_ro
--                     (à générer aléatoirement, ex. `openssl rand -base64 24`)
--                     puis à reporter dans secrets.prod-beta-internal.local.yaml
--                     sous admin-int-db-secret.ADMIN_INT_DB_PASSWORD
--
-- Exécution :
--   ADMIN_PW="$(openssl rand -base64 24)"
--   kubectl cp migrations/internal/001_admin_readonly_role.sql \
--     audio-internal/postgres-internal-0:/tmp/001.sql
--   kubectl -n audio-internal exec -i postgres-internal-0 -- \
--     psql -U audio_int -d audio_upload_int \
--          -v admin_password="$ADMIN_PW" -f /tmp/001.sql
--   echo "Reporter $ADMIN_PW dans admin-int-db-secret.ADMIN_INT_DB_PASSWORD"
-- ============================================================================

\set ON_ERROR_STOP on

-- Création conditionnelle du rôle (idempotent via \gexec)
-- Si le rôle n'existe pas → CREATE ; sinon → ALTER pour rotater le password
SELECT format('CREATE ROLE audio_admin_ro WITH LOGIN PASSWORD %L', :'admin_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audio_admin_ro')
\gexec

SELECT format('ALTER ROLE audio_admin_ro WITH LOGIN PASSWORD %L', :'admin_password')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audio_admin_ro')
\gexec

-- Grants idempotents (GRANT n'erre pas si déjà accordé)
GRANT CONNECT ON DATABASE audio_upload_int TO audio_admin_ro;
GRANT USAGE ON SCHEMA public TO audio_admin_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO audio_admin_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO audio_admin_ro;

-- Tables/sequences créées plus tard auront aussi le SELECT par défaut
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO audio_admin_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO audio_admin_ro;

-- Vérification finale
SELECT
  rolname,
  rolcanlogin,
  (SELECT count(*) FROM information_schema.role_table_grants
    WHERE grantee = rolname AND privilege_type = 'SELECT') AS nb_select_grants
FROM pg_roles
WHERE rolname = 'audio_admin_ro';
