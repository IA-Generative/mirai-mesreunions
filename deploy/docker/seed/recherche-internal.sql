-- Jeu d'essai LOCAL de la recherche (contrat de recherche MirAI) — zone interne.
--
-- Données FICTIVES. Ne jamais appliquer hors d'un environnement de dev.
-- Prérequis : tables créées (démarrage d'internal-ingester ou migrations
-- internes), migration 024 appliquée.
--
--   psql -v user_a=<sub A> -v user_b=<sub B> -f recherche-internal.sql
--
-- Sans -v, deux sub fictifs sont utilisés. Pour tester avec un vrai jeton du
-- Keycloak local, passer en user_a le `sub` de l'utilisateur connecté.
-- À rejouer avec recherche-external.sql (mêmes valeurs de user_a/user_b).
--
-- Cas couverts (requête « budget ») :
--   …a1  upload de A, visible — blocs horodatés, émoji avant un terme (UTF-16)
--   …a2  upload de A, FICHIER à la corbeille (zone externe) — jamais renvoyé
--   …a3  upload de A, SESSION à la corbeille (zone externe) — jamais renvoyé
--        (visibilité fermée par défaut : seuls les uploads vivants listés par
--        mesreunions-web sortent)
--   …a4  import YouTube de A, réunion à la corbeille — jamais renvoyé
--   …a5  import YouTube de A, durée, date de réunion et locuteurs nuls (pas de
--        speaker_tagged_text : passage trouvé sans position)
--   …a6  import YouTube ré-importé : meeting_id pointe l'ancienne réunion à la
--        corbeille, la nouvelle réunion vivante le référence par
--        user_audio_file_id — visible
--   …a7  upload de A supprimé définitivement : plus de ligne en zone externe,
--        la ligne interne subsiste — jamais renvoyé
--   …a8  import de A dont la réunion a été purgée (meeting_id orphelin) —
--        jamais renvoyé
--   …b1  upload de B — jamais renvoyé à A (isolation)
--
-- Participants de …a1 : 3 présents (participants_presents), 4 dans « actors »
-- (qui ajoute les personnes seulement citées) — le contexte doit dire 3.

\if :{?user_a}
\else
\set user_a 'recherche-test-user-a'
\endif
\if :{?user_b}
\else
\set user_b 'recherche-test-user-b'
\endif

BEGIN;

DELETE FROM meetings WHERE id IN (
    '5e5e0000-0000-4000-8000-0000000000f4',
    '5e5e0000-0000-4000-8000-0000000000f5',
    '5e5e0000-0000-4000-8000-0000000000f6',
    '5e5e0000-0000-4000-8000-0000000000f7');
DELETE FROM user_audio_files WHERE id::text LIKE '5e5e0000-0000-4000-8000-0000000000%';
DELETE FROM video_sources WHERE provider = 'youtube' AND provider_video_id LIKE 'rechTEST%';

INSERT INTO video_sources (id, provider, provider_video_id, canonical_url, title, channel, duration_sec)
VALUES
    (990004, 'youtube', 'rechTEST004', 'https://video.example/watch?v=rechTEST004',
     'Table ronde budget supprimée', 'Chaîne fictive', 1800),
    (990005, 'youtube', 'rechTEST005', 'https://video.example/watch?v=rechTEST005',
     'Conférence « Numérique de l''État 2027 »', 'Chaîne fictive', NULL),
    (990006, 'youtube', 'rechTEST006', 'https://video.example/watch?v=rechTEST006',
     'Audition sur les finances publiques', 'Chaîne fictive', 2400);

INSERT INTO user_audio_files (
    id, user_sub, original_session_code, original_filename, stored_filename,
    file_size_bytes, origin, source_type, external_video_source_id,
    transcription_status, suggested_filename, meeting_datetime,
    audio_duration_seconds, key_points_summary, meeting_analysis_json,
    speaker_tagged_text, cleaned_text, created_at,
    reprocess_version, reprocess_history, hidden_block_indices
) VALUES
(
    '5e5e0000-0000-4000-8000-0000000000a1', :'user_a', 'SRCH01', 'copil.m4a',
    :'user_a' || '/SRCH01/SRCH01_copil.mp4',
    1000, 'upload', 'upload', NULL, 'kevent_completed',
    'COPIL Nexus – septembre', '2026-09-19 10:00:00+02', 4320,
    E'- **Enveloppe 2027** fixée à 1,4 M€ sous réserve d''arbitrage.\n- Lot 2 décalé si décision après le 15 octobre.',
    '{"actors": [{"name": "M. Bertrand"}, {"name": "C. Martin"}, {"name": "J. Roux"}, {"name": "Secrétariat général"}],'
    ' "participants_presents": [{"name": "M. Bertrand"}, {"name": "C. Martin"}, {"name": "J. Roux"}],'
    ' "participants_cites": [{"name": "Secrétariat général"}]}',
    E'**M. Bertrand** _(23:41 → 24:10)_\n> 🎯 On part sur une enveloppe de 1,4 M€ pour 2027, sous réserve de l''arbitrage du SG sur le budget Nexus.\n\n'
    || E'**C. Martin** _(24:11 → 24:50)_\n> Il faut cet arbitrage avant le 15 octobre, sinon le lot 2 glisse au second semestre ; on en reparle à la prochaine réunion.\n\n'
    || E'**J. Roux** _(36:50 → 37:25)_\n> L''hébergement est chiffré sur le marché cadre, environ 310 k€ par an, à intégrer aux budgets.\n',
    E'M. Bertrand : On part sur une enveloppe de 1,4 M€ pour 2027, sous réserve de l''arbitrage du SG sur le budget Nexus. '
    || E'C. Martin : Il faut cet arbitrage avant le 15 octobre. J. Roux : hébergement à intégrer aux budgets.',
    '2026-09-19 12:00:00+02', 0, '[]', '[]'
),
(
    '5e5e0000-0000-4000-8000-0000000000a2', :'user_a', 'SRCH02', 'brouillon.m4a',
    :'user_a' || '/SRCH02/SRCH02_brouillon.mp4',
    1000, 'upload', 'upload', NULL, 'kevent_completed',
    'Brouillon supprimé – budget Nexus', '2026-09-01 10:00:00+02', 300, NULL, NULL,
    E'**X** _(0:10 → 0:20)_\n> Budget Nexus supprimé, ne doit jamais apparaître.\n',
    NULL, '2026-09-01 11:00:00+02', 0, '[]', '[]'
),
(
    '5e5e0000-0000-4000-8000-0000000000a3', :'user_a', 'SRCH03', 'session-supprimee.m4a',
    :'user_a' || '/SRCH03/SRCH03_session.mp4',
    1000, 'mobile', 'upload', NULL, 'kevent_completed',
    'Session supprimée – budget', '2026-09-02 10:00:00+02', 300, NULL, NULL,
    E'**X** _(0:10 → 0:20)_\n> Budget d''une session à la corbeille.\n',
    NULL, '2026-09-02 11:00:00+02', 0, '[]', '[]'
),
(
    '5e5e0000-0000-4000-8000-0000000000a4', :'user_a', 'YTrech04', 'Import YouTube', NULL,
    0, 'upload', 'youtube_subtitle', 990004, 'kevent_completed',
    NULL, NULL, NULL, NULL, NULL,
    E'**Intervenant_01** _(0:05 → 0:12)_\n> Débat sur le budget, import mis à la corbeille.\n',
    NULL, '2026-08-01 09:00:00+02', 0, '[]', '[]'
),
(
    '5e5e0000-0000-4000-8000-0000000000a5', :'user_a', 'YTrech05', 'Import YouTube', NULL,
    0, 'upload', 'youtube_subtitle', 990005, 'kevent_completed',
    NULL, NULL, NULL, NULL, NULL,
    NULL,
    E'Les grands projets numériques de l''État seront suivis de plus près à partir de 2027, notamment sur le budget.',
    '2026-06-03 08:00:00+02', 0, '[]', '[]'
),
(
    '5e5e0000-0000-4000-8000-0000000000a6', :'user_a', 'YTrech06', 'Import YouTube', NULL,
    0, 'upload', 'youtube_audio', 990006, 'kevent_completed',
    'Audition finances publiques', NULL, 2400, NULL, NULL,
    E'**Intervenant_01** _(2:00 → 2:30)_\n> Le budget de la mission est examiné ligne à ligne.\n\n'
    || E'**Intervenant_02** _(2:31 → 2:50)_\n> Nous répondrons par écrit sur les crédits.\n',
    NULL, '2026-07-10 08:00:00+02', 0, '[]', '[]'
),
(
    '5e5e0000-0000-4000-8000-0000000000b1', :'user_b', 'SRCH04', 'autre.m4a',
    :'user_b' || '/SRCH04/SRCH04_autre.mp4',
    1000, 'upload', 'upload', NULL, 'kevent_completed',
    'Réunion d''un autre agent – budget Nexus', '2026-09-20 10:00:00+02', 600, NULL, NULL,
    E'**Y** _(0:10 → 0:20)_\n> Budget Nexus, réunion privée d''un autre agent.\n',
    NULL, '2026-09-20 11:00:00+02', 0, '[]', '[]'
),
(
    '5e5e0000-0000-4000-8000-0000000000a7', :'user_a', 'SRCH05', 'efface.m4a',
    :'user_a' || '/SRCH05/SRCH05_efface.mp4',
    1000, 'upload', 'upload', NULL, 'kevent_completed',
    'Supprimé définitivement – budget', '2026-08-15 10:00:00+02', 300, NULL, NULL,
    E'**Z** _(0:10 → 0:20)_\n> Budget d''un upload supprimé définitivement.\n',
    NULL, '2026-08-15 11:00:00+02', 0, '[]', '[]'
),
(
    '5e5e0000-0000-4000-8000-0000000000a8', :'user_a', 'YTrech08', 'Import YouTube', NULL,
    0, 'upload', 'youtube_subtitle', NULL, 'kevent_completed',
    'Import purgé – budget', NULL, 900, NULL, NULL,
    E'**Intervenant_01** _(0:05 → 0:12)_\n> Budget d''un import dont la réunion a été purgée.\n',
    NULL, '2026-05-01 09:00:00+02', 0, '[]', '[]'
);

INSERT INTO meetings (id, user_sub, title, user_audio_file_id, video_source_id, created_at, trashed_at)
VALUES
    ('5e5e0000-0000-4000-8000-0000000000f4', :'user_a', 'Table ronde budget supprimée',
     '5e5e0000-0000-4000-8000-0000000000a4', 990004, now(), now()),
    ('5e5e0000-0000-4000-8000-0000000000f5', :'user_a', 'Conférence Numérique de l''État 2027',
     '5e5e0000-0000-4000-8000-0000000000a5', 990005, now(), NULL),
    ('5e5e0000-0000-4000-8000-0000000000f6', :'user_a', 'Audition (ancien import, corbeille)',
     '5e5e0000-0000-4000-8000-0000000000a6', 990006, now(), now()),
    ('5e5e0000-0000-4000-8000-0000000000f7', :'user_a', 'Audition (ré-import)',
     '5e5e0000-0000-4000-8000-0000000000a6', 990006, now(), NULL);

UPDATE user_audio_files SET meeting_id = '5e5e0000-0000-4000-8000-0000000000f4'
 WHERE id = '5e5e0000-0000-4000-8000-0000000000a4';
UPDATE user_audio_files SET meeting_id = '5e5e0000-0000-4000-8000-0000000000f5'
 WHERE id = '5e5e0000-0000-4000-8000-0000000000a5';
UPDATE user_audio_files SET meeting_id = '5e5e0000-0000-4000-8000-0000000000f6'
 WHERE id = '5e5e0000-0000-4000-8000-0000000000a6';
-- …a8 pointe une réunion qui n'existe plus (purgée définitivement).
UPDATE user_audio_files SET meeting_id = '5e5e0000-0000-4000-8000-0000000000f8'
 WHERE id = '5e5e0000-0000-4000-8000-0000000000a8';

COMMIT;
