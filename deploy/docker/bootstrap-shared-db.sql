-- Bootstrap des deux databases mirai-mesreunions dans le cluster
-- Postgres owuicore-postgres-1 partagé.
--
-- Application (depuis l'host) :
--   docker exec -i owuicore-postgres-1 psql -U owui -d postgres \
--     < deploy/docker/bootstrap-shared-db.sql
--
-- Idempotent : peut être rejoué sans dommage.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'audio_ext') THEN
    CREATE ROLE audio_ext LOGIN PASSWORD 'audio_ext_dev';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'audio_int') THEN
    CREATE ROLE audio_int LOGIN PASSWORD 'audio_int_dev';
  END IF;
END$$;

SELECT 'CREATE DATABASE audio_upload_ext OWNER audio_ext ENCODING UTF8'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'audio_upload_ext')
\gexec

SELECT 'CREATE DATABASE audio_upload_int OWNER audio_int ENCODING UTF8'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'audio_upload_int')
\gexec

GRANT ALL PRIVILEGES ON DATABASE audio_upload_ext TO audio_ext;
GRANT ALL PRIVILEGES ON DATABASE audio_upload_int TO audio_int;
