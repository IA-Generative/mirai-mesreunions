-- Persiste les mesures loudnorm (source + normalisé) pour éviter de
-- redownloader la source S3 après purge quand l'utilisateur consulte
-- l'impact de la normalisation depuis mydevices.
--
-- Idempotent : "ADD COLUMN IF NOT EXISTS" (PostgreSQL 9.6+).

ALTER TABLE uploaded_files
    ADD COLUMN IF NOT EXISTS normalization_source_i  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS normalization_source_tp DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS normalization_source_lra DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS normalization_output_i  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS normalization_output_tp DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS normalization_output_lra DOUBLE PRECISION;
