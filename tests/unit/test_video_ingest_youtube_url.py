"""Tests du parseur d'URL YouTube (cœur de la dédup video-ingest).

100% pur, zéro réseau. Couvre les formats supportés, les variantes de
scheme/host, et les rejets explicites (playlist seule, host inconnu,
video_id mal formé).
"""

import pytest

from services.video_ingest.app.providers.youtube import url as yt_url


CANONICAL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.mark.parametrize("raw", [
    # watch?v= sous toutes ses variantes de host
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ",
    "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    # Paramètres parasites (timestamp, playlist, index, source)
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxxx&index=3",
    "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
    # youtu.be raccourci
    "https://youtu.be/dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?t=42",
    "youtu.be/dQw4w9WgXcQ",  # sans scheme
    # /shorts, /embed, /live
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ?autoplay=1",
    "https://www.youtube.com/live/dQw4w9WgXcQ",
    # Sans scheme
    "www.youtube.com/watch?v=dQw4w9WgXcQ",
])
def test_parse_normalizes_to_canonical(raw):
    result = yt_url.parse(raw)
    assert result.video_id == "dQw4w9WgXcQ"
    assert result.canonical_url == CANONICAL


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "https://vimeo.com/12345",
    "https://www.dailymotion.com/video/x7tgad0",
    "https://www.youtube.com/playlist?list=PLxxx",  # playlist seule = rejet
    "https://www.youtube.com/watch",                # pas de v=
    "https://www.youtube.com/watch?v=",             # v= vide
    "https://www.youtube.com/watch?v=trop_court",   # 11 chars requis
    "https://www.youtube.com/watch?v=ID_avec_caracteres_invalides!",
    "https://youtu.be/",                            # path vide
    "https://www.youtube.com/channel/UCxxxx",       # chaîne, pas vidéo
])
def test_parse_rejects(raw):
    with pytest.raises(yt_url.YouTubeUrlError):
        yt_url.parse(raw)


def test_parse_rejects_non_string():
    with pytest.raises(yt_url.YouTubeUrlError):
        yt_url.parse(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw,expected", [
    ("https://www.youtube.com/watch?v=anything", True),
    ("youtu.be/xxx", True),
    ("https://m.youtube.com/", True),
    ("https://music.youtube.com/playlist?list=x", True),
    ("https://vimeo.com/123", False),
    ("https://example.com", False),
    ("", False),
    ("   ", False),
])
def test_matches(raw, expected):
    assert yt_url.matches(raw) is expected


def test_canonical_is_idempotent():
    """parse(canonical) doit redonner exactement le même canonical."""
    once = yt_url.parse(CANONICAL)
    twice = yt_url.parse(once.canonical_url)
    assert once == twice
