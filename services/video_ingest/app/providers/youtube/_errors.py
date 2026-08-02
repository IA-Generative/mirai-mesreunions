"""Classification des erreurs YouTube : transitoire vs terminal.

Point unique de vérité partagé par `metadata.py`, `audio.py` et
`subtitles.py` — sans ça chaque module re-devinait ses propres mots-clés
et l'anti-bot tombait dans le fourre-tout `ProviderError` (= terminal).

Incident 2026-08-02 : la même vidéo échoue à 09:59 sur
« Sign in to confirm you're not a bot » et passe à 10:03. La réputation
de l'IP d'egress (NAT SCW mutualisée entre clusters Kapsule) flappe ;
l'échec est donc *retryable*, pas définitif.
"""

from __future__ import annotations

import re
import unicodedata

from ..base import ProviderError, TransientProviderError, VideoUnavailable

# Transitoire : rejeu utile. Testé en premier — un message anti-bot ne
# contient aucun des mots terminaux, mais l'ordre rend l'intention claire
# et protège d'un futur wording YouTube qui mélangerait les deux.
_TRANSIENT_PATTERNS = (
    r"confirm you'?re not a bot",     # anti-bot (le classique)
    r"sign in to confirm",            # variante du même mur
    r"http error 429",
    r"too many requests",
    r"rate[ -]?limit",
    r"http error 5\d\d",              # 500/502/503/504 côté YouTube
    r"service unavailable",
    r"please try again later",
    r"temporary failure",
    r"connection reset",
    r"read timed out",
    r"timed? out",
    r"\bipblocked\b",                 # youtube-transcript-api
    r"\brequestblocked\b",            # youtube-transcript-api
    r"youtube is blocking requests",  # wording youtube-transcript-api
)

# Terminal : la vidéo n'est pas récupérable, rejouer ne sert à rien.
_UNAVAILABLE_PATTERNS = (
    r"\bprivate\b",
    r"\bremoved\b",
    r"\bunavailable\b",
    r"\bblocked\b",
    r"does not exist",
    r"has been terminated",
    r"age[- ]restricted",
)


def _normalize(msg: str) -> str:
    """Minuscules + apostrophes typographiques ramenées à l'ASCII.

    yt-dlp émet « you’re » (U+2019), pas « you're » — sans cette
    normalisation le motif anti-bot ne matchait jamais.
    """
    return unicodedata.normalize("NFKC", msg).replace("’", "'").lower()


def is_transient(msg: str) -> bool:
    norm = _normalize(msg)
    return any(re.search(p, norm) for p in _TRANSIENT_PATTERNS)


def classify(msg: str, *, prefix: str = "") -> ProviderError:
    """Renvoie l'exception à lever pour un message d'erreur brut.

    `prefix` contextualise le message final (ex. « yt-dlp download failed »).
    """
    text = f"{prefix}: {msg}" if prefix else msg
    norm = _normalize(msg)
    if any(re.search(p, norm) for p in _TRANSIENT_PATTERNS):
        return TransientProviderError(text)
    if any(re.search(p, norm) for p in _UNAVAILABLE_PATTERNS):
        return VideoUnavailable(text)
    return ProviderError(text)
