"""Parsing et normalisation des URLs YouTube.

Module pur (zéro I/O réseau, zéro dépendance externe) : c'est la brique
critique de la déduplication (cf. Principe 4 — l'unicité d'une vidéo est
(provider, provider_video_id), pas l'URL brute).

Formats supportés :
- https://www.youtube.com/watch?v=ID
- https://youtube.com/watch?v=ID
- https://m.youtube.com/watch?v=ID
- https://youtu.be/ID
- https://www.youtube.com/shorts/ID
- https://www.youtube.com/embed/ID
- avec ou sans `&t=`, `&list=`, `&index=`, etc.
- avec ou sans scheme, avec ou sans `www.`

Note : les URLs de playlist seule (`/playlist?list=...`) sont REJETÉES.
L'import d'une playlist passera par une route dédiée qui éclate la
playlist en N URLs vidéo (V1.5+).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
}
_YOUTU_BE_HOSTS = {"youtu.be", "www.youtu.be"}

# Un video_id YouTube fait toujours 11 caractères dans l'alphabet ci-dessous.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class ParsedYouTubeUrl:
    video_id: str
    canonical_url: str  # https://www.youtube.com/watch?v=<id>


class YouTubeUrlError(ValueError):
    """URL non reconnue comme une vidéo YouTube exploitable."""


def parse(url: str) -> ParsedYouTubeUrl:
    """Extrait le video_id et produit l'URL canonique.

    Lève `YouTubeUrlError` si l'URL n'est pas une vidéo YouTube valide
    (playlist seule, host inconnu, id absent ou mal formé).
    """
    if not url or not isinstance(url, str):
        raise YouTubeUrlError("URL vide ou non textuelle")

    raw = url.strip()
    # Tolérer l'absence de scheme : `youtu.be/xxx`, `youtube.com/watch?v=xxx`.
    if "://" not in raw:
        raw = "https://" + raw

    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    video_id: str | None = None

    if host in _YOUTU_BE_HOSTS:
        # https://youtu.be/<id>[?t=...]
        candidate = path.lstrip("/").split("/", 1)[0]
        video_id = candidate or None
    elif host in _YOUTUBE_HOSTS:
        if path == "/watch":
            qs = parse_qs(parsed.query)
            v = qs.get("v")
            if v:
                video_id = v[0]
        elif path.startswith("/shorts/"):
            video_id = path[len("/shorts/") :].split("/", 1)[0]
        elif path.startswith("/embed/"):
            video_id = path[len("/embed/") :].split("/", 1)[0]
        elif path.startswith("/live/"):
            video_id = path[len("/live/") :].split("/", 1)[0]
        # /playlist sans /watch est rejeté par construction (video_id reste None)

    if not video_id or not _VIDEO_ID_RE.match(video_id):
        raise YouTubeUrlError(f"video_id YouTube introuvable ou invalide dans : {url!r}")

    canonical = f"https://www.youtube.com/watch?v={video_id}"
    return ParsedYouTubeUrl(video_id=video_id, canonical_url=canonical)


def matches(url: str) -> bool:
    """Test rapide : l'URL ressemble-t-elle à une vidéo YouTube ?

    Utilisé par le routage multi-providers (`VideoProvider.matches_url`).
    Ne valide PAS le video_id (parse() le fait).
    """
    if not url or not isinstance(url, str):
        return False
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return False
    return host in _YOUTUBE_HOSTS or host in _YOUTU_BE_HOSTS
