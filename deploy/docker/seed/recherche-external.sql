-- Jeu d'essai LOCAL de la recherche (contrat de recherche MirAI) — zone externe.
--
-- Données FICTIVES. Ne jamais appliquer hors d'un environnement de dev.
-- Sessions et fichiers des uploads de recherche-internal.sql : c'est ici que
-- se décide quels uploads sont vivants (la zone interne n'en sait rien).
--
--   psql -v user_a=<sub A> -v user_b=<sub B> -f recherche-external.sql
--
--   SRCH01  session active, fichier visible          → …a1 renvoyé
--   SRCH02  session active, FICHIER à la corbeille   → …a2 exclu
--   SRCH03  SESSION à la corbeille, fichier intact   → …a3 exclu
--   SRCH04  session de B                             → …b1 jamais vu par A
--   SRCH05  absent : supprimé définitivement         → …a7 exclu

\if :{?user_a}
\else
\set user_a 'recherche-test-user-a'
\endif
\if :{?user_b}
\else
\set user_b 'recherche-test-user-b'
\endif

BEGIN;

DELETE FROM uploaded_files WHERE session_id IN (
    SELECT id FROM upload_sessions WHERE simple_code IN ('SRCH01', 'SRCH02', 'SRCH03', 'SRCH04'));
DELETE FROM upload_sessions WHERE simple_code IN ('SRCH01', 'SRCH02', 'SRCH03', 'SRCH04');

INSERT INTO upload_sessions (id, user_sub, simple_code, qr_token, status, max_uploads,
                             upload_count, ttl_minutes, expires_at, created_at, updated_at, trashed_at,
                             failed_attempts)
VALUES
    ('5e5e0000-0000-4000-8000-0000000001a1', :'user_a', 'SRCH01', 'recherche-test-qr-01', 'ACTIVE',
     5, 1, 15, now() + interval '7 days', now(), now(), NULL, 0),
    ('5e5e0000-0000-4000-8000-0000000001a2', :'user_a', 'SRCH02', 'recherche-test-qr-02', 'ACTIVE',
     5, 1, 15, now() + interval '7 days', now(), now(), NULL, 0),
    ('5e5e0000-0000-4000-8000-0000000001a3', :'user_a', 'SRCH03', 'recherche-test-qr-03', 'ACTIVE',
     5, 1, 15, now() + interval '7 days', now(), now(), now(), 0),
    ('5e5e0000-0000-4000-8000-0000000001b1', :'user_b', 'SRCH04', 'recherche-test-qr-04', 'ACTIVE',
     5, 1, 15, now() + interval '7 days', now(), now(), NULL, 0);

INSERT INTO uploaded_files (id, session_id, original_filename, stored_filename, file_size_bytes,
                            status, transcoded_filename, audio_duration_seconds,
                            created_at, updated_at, trashed_at)
VALUES
    ('5e5e0000-0000-4000-8000-0000000002a1', '5e5e0000-0000-4000-8000-0000000001a1',
     'copil.m4a', 'SRCH01_copil.m4a', 1000, 'TRANSFERRED', 'SRCH01_copil.mp4', 4320,
     now(), now(), NULL),
    ('5e5e0000-0000-4000-8000-0000000002a2', '5e5e0000-0000-4000-8000-0000000001a2',
     'brouillon.m4a', 'SRCH02_brouillon.m4a', 1000, 'TRANSFERRED', 'SRCH02_brouillon.mp4', 300,
     now(), now(), now()),
    ('5e5e0000-0000-4000-8000-0000000002a3', '5e5e0000-0000-4000-8000-0000000001a3',
     'session-supprimee.m4a', 'SRCH03_session.m4a', 1000, 'TRANSFERRED', 'SRCH03_session.mp4', 300,
     now(), now(), NULL),
    ('5e5e0000-0000-4000-8000-0000000002b1', '5e5e0000-0000-4000-8000-0000000001b1',
     'autre.m4a', 'SRCH04_autre.m4a', 1000, 'TRANSFERRED', 'SRCH04_autre.mp4', 600,
     now(), now(), NULL);

COMMIT;
