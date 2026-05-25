-- 019 — video-ingest : schéma initial du service mutualisé d'ingestion
-- vidéo (cf. features-2-build/FEATURE_video-ingest.md, D14).
--
-- Principe : toutes les tables sont préfixées `video_*`, aucune FK vers
-- les tables MirAI (user_audio_files, meetings, briefs, …). L'identité
-- utilisateur est portée par un champ `user_sub` opaque (sub OIDC),
-- exactement comme les clients (Mes Réunions, Mes Collections) la
-- véhiculeront via l'API REST/MCP. Objectif : extraction du composant
-- vers son propre repo via `git filter-repo` sans avoir à démêler des
-- jointures cross-domain.
--
-- Idempotent : IF NOT EXISTS partout, rejouable sans effet.

BEGIN;

-- ─── VideoSource ────────────────────────────────────────────────────────
-- Une ligne par vidéo unique sur le web. Partagée entre tous les
-- utilisateurs (cache mutualisé). Unicité = (provider, provider_video_id)
-- APRÈS normalisation de l'URL côté provider.
CREATE TABLE IF NOT EXISTS video_sources (
    id                  BIGSERIAL PRIMARY KEY,
    provider            VARCHAR(32) NOT NULL,
    provider_video_id   VARCHAR(64) NOT NULL,
    canonical_url       TEXT        NOT NULL,
    title               TEXT,
    channel             TEXT,
    duration_sec        INTEGER,
    published_at        TIMESTAMPTZ,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata_json       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT video_sources_provider_id_unique
        UNIQUE (provider, provider_video_id)
);

-- ─── Transcript ─────────────────────────────────────────────────────────
-- Attaché à un VideoSource. Plusieurs transcripts possibles par source
-- (langues différentes, méthodes différentes). Le full-text PostgreSQL
-- (tsvector français) sert la recherche V1 — pas de RAG (D6/D7).
CREATE TABLE IF NOT EXISTS video_transcripts (
    id                  BIGSERIAL PRIMARY KEY,
    video_source_id     BIGINT      NOT NULL REFERENCES video_sources(id) ON DELETE CASCADE,
    language            VARCHAR(8)  NOT NULL,
    method              VARCHAR(32) NOT NULL,    -- subtitle_manual | subtitle_auto | asr_whisper_v3 | ...
    content_text        TEXT        NOT NULL,    -- post-traité (V1 = identique à _raw)
    content_text_raw    TEXT        NOT NULL,    -- avant post-traitement LLM (V1.5)
    segments_json       JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- chunks temporels start_seconds/end_seconds
    content_tsv         TSVECTOR
                        GENERATED ALWAYS AS (to_tsvector('french', coalesce(content_text, ''))) STORED,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT video_transcripts_source_lang_method_unique
        UNIQUE (video_source_id, language, method)
);

CREATE INDEX IF NOT EXISTS ix_video_transcripts_tsv
    ON video_transcripts USING GIN (content_tsv);

CREATE INDEX IF NOT EXISTS ix_video_transcripts_source
    ON video_transcripts (video_source_id);

-- ─── UserVideoBookmark ──────────────────────────────────────────────────
-- Matérialise l'appropriation d'un VideoSource par un utilisateur dans
-- un contexte (meeting, collection, …). Pas de FK vers les tables MirAI
-- côté `context_id` : c'est volontairement un identifiant opaque côté
-- video-ingest, le client est responsable de sa résolution.
CREATE TABLE IF NOT EXISTS user_video_bookmarks (
    id                  BIGSERIAL PRIMARY KEY,
    user_sub            VARCHAR(255) NOT NULL,    -- sub OIDC opaque
    video_source_id     BIGINT       NOT NULL REFERENCES video_sources(id) ON DELETE CASCADE,
    bookmarked_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    context             VARCHAR(32),              -- meeting | collection | null
    context_id          VARCHAR(128),             -- opaque, propre au client
    user_notes          TEXT
);

CREATE INDEX IF NOT EXISTS ix_user_video_bookmarks_user
    ON user_video_bookmarks (user_sub, bookmarked_at DESC);

CREATE INDEX IF NOT EXISTS ix_user_video_bookmarks_context
    ON user_video_bookmarks (context, context_id)
    WHERE context IS NOT NULL;

-- ─── video_ingest_jobs ──────────────────────────────────────────────────
-- File de jobs gérée en Postgres natif (D13) : `SELECT ... FOR UPDATE
-- SKIP LOCKED` côté worker, `LISTEN/NOTIFY` pour réveil immédiat. Pas
-- de RabbitMQ, pas de Celery — zéro nouvelle dépendance.
--
-- Cycle de vie : pending → running → done | failed.
-- `lease_until` = visibility timeout (relâché par le watchdog si dépassé).
CREATE TABLE IF NOT EXISTS video_ingest_jobs (
    id                  BIGSERIAL PRIMARY KEY,
    url                 TEXT         NOT NULL,    -- URL brute fournie par le client
    user_sub            VARCHAR(255) NOT NULL,
    context             VARCHAR(32),
    context_id          VARCHAR(128),
    language_pref       VARCHAR(8),               -- fr | en | null
    force_audio         BOOLEAN      NOT NULL DEFAULT FALSE,
    status              VARCHAR(16)  NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
    video_source_id     BIGINT       REFERENCES video_sources(id) ON DELETE SET NULL,
    reused              BOOLEAN,                  -- renseigné à terminal : true si hit cache
    error_message       TEXT,
    attempts            INTEGER      NOT NULL DEFAULT 0,
    lease_until         TIMESTAMPTZ,              -- visibility timeout
    claimed_by          VARCHAR(128),             -- identifiant worker (pod hostname)
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

-- Hot path worker : récupérer le prochain job pending (ou running expiré).
CREATE INDEX IF NOT EXISTS ix_video_ingest_jobs_dispatch
    ON video_ingest_jobs (status, lease_until NULLS FIRST, created_at)
    WHERE status IN ('pending', 'running');

CREATE INDEX IF NOT EXISTS ix_video_ingest_jobs_user
    ON video_ingest_jobs (user_sub, created_at DESC);

COMMIT;
