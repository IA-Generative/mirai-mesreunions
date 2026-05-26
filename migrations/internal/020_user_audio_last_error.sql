-- 020 — Colonnes d'observabilité d'erreur sur user_audio_files.
--
-- Aujourd'hui la table ne garde aucune trace de POURQUOI un job a échoué :
-- ni code HTTP upstream (Kevent, MCR), ni exception ffmpeg, ni signature
-- d'erreur. Conséquence : impossible de débugger à partir de la DB, et
-- impossible de surfacer à l'utilisateur un message UI actionnable.
--
-- Ces colonnes sont peuplées par TOUS les call-sites qui passent une row en
-- statut terminal d'échec (mcr_import_failed, kevent_failed, etc) :
--   - services/dmz-to-internal-bridge/app/mcr_importer.py:_set_failed()
--   - services/dmz-to-internal-bridge/app/pipeline_watchdog.py:_mark_capped_as_failed()
--   - services/dmz-to-internal-bridge/app/puller.py (Kevent path)
--
-- last_error_kind : code court (≤64 chars) pour switch côté UI/alerting.
--   Catalogue : mcr_audio_404, mcr_transcript_404, mcr_unavailable_on_source,
--   mcr_auth_failed, mcr_http_5xx, ffmpeg_transcode_failed, s3_upload_failed,
--   kevent_4xx, kevent_5xx, kevent_timeout, kevent_samples_mismatch,
--   diarization_failed, llm_failed, claim_lost, worker_crash, cap_exceeded.
-- last_error_message : texte libre (réponse upstream tronquée, exception, etc).
-- last_error_at : horodatage du dernier échec, pour ordering / TTL alerting.
--
-- Idempotent : IF NOT EXISTS partout, rejouable sans effet.

BEGIN;

ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;
ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS last_error_kind VARCHAR(64);
ALTER TABLE user_audio_files
    ADD COLUMN IF NOT EXISTS last_error_message TEXT;

COMMIT;
