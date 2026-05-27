"""Repositories raw SQL pour video_sources / video_transcripts /
user_video_bookmarks (cf. migration 019).

Pas d'ORM (D14 : minimiser les deps + transparence).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Iterable

from psycopg2.extras import Json

from .types import FetchedTranscript, VideoMetadata


def find_source_by_provider_id(
    conn, *, provider: str, provider_video_id: str
) -> int | None:
    """Lookup dédup (Principe 4). Renvoie l'id si HIT, None si MISS."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM video_sources WHERE provider = %s AND provider_video_id = %s",
            (provider, provider_video_id),
        )
        row = cur.fetchone()
        return row[0] if row else None


def upsert_source(conn, meta: VideoMetadata) -> int:
    """Insère ou met à jour une source. Renvoie l'id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO video_sources (
                provider, provider_video_id, canonical_url,
                title, channel, duration_sec, published_at, metadata_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (provider, provider_video_id) DO UPDATE
               SET title         = COALESCE(EXCLUDED.title, video_sources.title),
                   channel       = COALESCE(EXCLUDED.channel, video_sources.channel),
                   duration_sec  = COALESCE(EXCLUDED.duration_sec, video_sources.duration_sec),
                   published_at  = COALESCE(EXCLUDED.published_at, video_sources.published_at),
                   metadata_json = EXCLUDED.metadata_json,
                   fetched_at    = NOW()
            RETURNING id
            """,
            (
                meta.provider, meta.provider_video_id, meta.canonical_url,
                meta.title, meta.channel, meta.duration_sec, meta.published_at,
                Json(meta.extra),
            ),
        )
        return cur.fetchone()[0]


def insert_transcript(
    conn,
    *,
    video_source_id: int,
    transcript: FetchedTranscript,
    segments_json: list[dict],
    content_text: str,
    content_text_raw: str,
) -> int:
    """Insère un transcript. Si la combo (source, langue, method) existe
    déjà (ON CONFLICT), on ne ré-insère pas — la dédup au niveau Transcript
    est gérée explicitement par le caller (V1.5 `refetch`)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO video_transcripts (
                video_source_id, language, method,
                content_text, content_text_raw, segments_json
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (video_source_id, language, method) DO UPDATE
               SET content_text     = EXCLUDED.content_text,
                   content_text_raw = EXCLUDED.content_text_raw,
                   segments_json    = EXCLUDED.segments_json
            RETURNING id
            """,
            (
                video_source_id, transcript.language, transcript.method,
                content_text, content_text_raw, Json(segments_json),
            ),
        )
        return cur.fetchone()[0]


def load_source(conn, video_source_id: int) -> dict | None:
    """Charge les metadata d'une source vidéo existante. Utilisé en HIT
    cache pour re-déclencher materialize sans re-fetch YouTube."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT provider, provider_video_id, canonical_url, title, channel,
                      duration_sec
                 FROM video_sources WHERE id = %s""",
            (video_source_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "provider": row[0],
            "provider_video_id": row[1],
            "canonical_url": row[2],
            "title": row[3],
            "channel": row[4],
            "duration_sec": row[5],
        }


def load_best_transcript(conn, video_source_id: int, *, language: str | None = None) -> dict | None:
    """Charge le meilleur transcript existant pour une source (manuels
    prioritaires, fallback sur la langue demandée puis n'importe laquelle)."""
    with conn.cursor() as cur:
        if language:
            cur.execute(
                """SELECT id, language, method, content_text, segments_json
                     FROM video_transcripts
                    WHERE video_source_id = %s AND language = %s
                    ORDER BY (method = 'subtitle_manual') DESC, created_at DESC LIMIT 1""",
                (video_source_id, language),
            )
        else:
            cur.execute(
                """SELECT id, language, method, content_text, segments_json
                     FROM video_transcripts
                    WHERE video_source_id = %s
                    ORDER BY (method = 'subtitle_manual') DESC, created_at DESC LIMIT 1""",
                (video_source_id,),
            )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "language": row[1],
            "method": row[2],
            "content_text": row[3],
            "segments_json": row[4] or [],
        }


def add_bookmark(
    conn,
    *,
    user_sub: str,
    video_source_id: int,
    context: str | None,
    context_id: str | None,
    user_notes: str | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_video_bookmarks (
                user_sub, video_source_id, context, context_id, user_notes
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_sub, video_source_id, context, context_id, user_notes),
        )
        return cur.fetchone()[0]


def has_transcript(
    conn, *, video_source_id: int, language: str | None = None
) -> bool:
    """Le HIT cache nécessite à la fois une source ET au moins un
    transcript exploitable (sinon on doit refaire le fetch)."""
    with conn.cursor() as cur:
        if language:
            cur.execute(
                "SELECT 1 FROM video_transcripts WHERE video_source_id = %s AND language = %s LIMIT 1",
                (video_source_id, language),
            )
        else:
            cur.execute(
                "SELECT 1 FROM video_transcripts WHERE video_source_id = %s LIMIT 1",
                (video_source_id,),
            )
        return cur.fetchone() is not None
