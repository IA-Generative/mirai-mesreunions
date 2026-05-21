"""Extraction d'une date/heure de réunion depuis le nom de fichier audio.

Les dictaphones et apps de notes (Voice Memos iOS, Cop'ia, Easy Voice
Recorder Android, ffmpeg presets) intègrent fréquemment la date de
l'enregistrement dans le nom. Cette extraction permet de pré-remplir
``meeting_datetime`` côté UserAudioFile sans demander à l'utilisateur.

Politique :
- Best-effort : si aucun pattern ne matche, renvoie ``None`` (silencieux).
- L'override manuel utilisateur prime toujours sur le résultat de cette
  fonction (le caller ne doit appeler que si ``meeting_datetime IS NULL``).
- Fuseau : on présume Europe/Paris car c'est le contexte cible (utilisateurs
  ministère de l'Intérieur, télétravail majoritairement métropole). On
  expose un paramètre ``tz`` pour personnaliser.
- Validation : refuse les dates futures ou trop anciennes (< 2000) pour
  filtrer les faux positifs (ex: filename "2099" ou "v1023").

Patterns supportés (par ordre de priorité décroissante) :
    1. ISO 8601 avec séparateurs flexibles entre date et heure :
       2026-05-21_14_29_49, 2026-05-21T14:30:00, 2026/05/21 14:30
    2. Compact YYYYMMDD_HHMMSS (sans séparateurs internes) :
       20260521_143000, 20260521-143000
    3. Européen DD-MM-YYYY avec heure :
       21-05-2026_14h30, 21/05/2026 14:30
    4. Date seule (ISO ou européen) :
       2026-05-21, 21-05-2026 → heure mise à 00:00
    5. Préfixe français "Enregistrement du DD-MM-YYYY à HHhMM" :
       Enregistrement_du_21-05-2026_à_14h30
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Optional


# Sentinelle : la datetime parsée doit être dans cette fenêtre raisonnable
# pour être acceptée (filtre les faux positifs comme "v1023" ou "9999").
_MIN_YEAR = 2000
# Tolérance dans le futur (1 jour) pour les uploads en différé de fuseau.
_MAX_FUTURE_DAYS = 1


# Pattern 1 : ISO 8601 flexible.
# Date YYYY-MM-DD avec séparateur [- _ /], puis optionnellement heure
# HH-MM(-SS) séparée par [ T _ -], composants horaires séparés par [- _ : h].
_ISO_DATETIME_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>20\d{2})[-_/]"
    r"(?P<month>0[1-9]|1[0-2])[-_/]"
    r"(?P<day>0[1-9]|[12]\d|3[01])"
    r"(?:"
    r"[ T_-]+"
    r"(?P<hour>[01]\d|2[0-3])[-_h:]"
    r"(?P<minute>[0-5]\d)"
    r"(?:[-_:](?P<second>[0-5]\d))?"
    r")?"
    r"(?!\d)"
)

# Pattern 2 : compact YYYYMMDD_HHMMSS, séparateur _ ou - entre date et heure.
_COMPACT_DATETIME_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>20\d{2})"
    r"(?P<month>0[1-9]|1[0-2])"
    r"(?P<day>0[1-9]|[12]\d|3[01])"
    r"(?:"
    r"[_-]"
    r"(?P<hour>[01]\d|2[0-3])"
    r"(?P<minute>[0-5]\d)"
    r"(?:(?P<second>[0-5]\d))?"
    r")?"
    r"(?!\d)"
)

# Pattern 3 : européen DD-MM-YYYY (avec heure HHhMM ou HH:MM ou HH-MM).
_EU_DATETIME_RE = re.compile(
    r"(?<!\d)"
    r"(?P<day>0[1-9]|[12]\d|3[01])[-/_.]"
    r"(?P<month>0[1-9]|1[0-2])[-/_.]"
    r"(?P<year>20\d{2})"
    r"(?:"
    r"[ T_-]+"
    r"(?:à[ _])?"
    r"(?P<hour>[01]\d|2[0-3])[-_h:]"
    r"(?P<minute>[0-5]\d)"
    r"(?:[-_:](?P<second>[0-5]\d))?"
    r")?"
    r"(?!\d)"
)


def _build_dt(year: int, month: int, day: int,
              hour: int = 0, minute: int = 0, second: int = 0,
              tz: timezone = None) -> Optional[datetime]:
    """Construit une datetime ; renvoie None si invalide (29 février non-bissextile, etc)."""
    try:
        return datetime(year, month, day, hour, minute, second,
                        tzinfo=tz or timezone.utc)
    except ValueError:
        return None


def _validate(dt: datetime) -> bool:
    """Filtre les dates aberrantes (passées trop loin, futures trop loin)."""
    if dt.year < _MIN_YEAR:
        return False
    now = datetime.now(timezone.utc)
    if dt > now + timedelta(days=_MAX_FUTURE_DAYS):
        return False
    return True


def extract_meeting_datetime(
    filename: str,
    tz: Optional[timezone] = None,
) -> Optional[datetime]:
    """Tente d'extraire une date/heure de réunion depuis ``filename``.

    Renvoie un ``datetime`` timezone-aware (par défaut Europe/Paris
    représenté en UTC+02:00 — pour rester sans dépendance ``zoneinfo``,
    on hardcode l'offset CEST. Pour un calcul précis DST, le caller
    peut passer ``tz=ZoneInfo("Europe/Paris")``).

    Renvoie ``None`` si :
    - aucun pattern ne matche
    - le pattern matche mais produit une date invalide (29 février
      non-bissextile, mois 13, etc.)
    - la date est aberrante (avant 2000 ou plus d'1 jour dans le futur)
    """
    if not filename or not isinstance(filename, str):
        return None
    # Défaut : Europe/Paris été (CEST = UTC+2). Pour le contexte d'usage
    # (utilisateurs FR métropole) c'est largement suffisant — les écarts
    # CET/CEST d'1h en hiver sont acceptables car l'heure est de toute
    # façon éditable manuellement par l'utilisateur après coup.
    if tz is None:
        tz = timezone(timedelta(hours=2))

    # Strip extension pour ne pas matcher "021" dans ".mp3" comme une année.
    base = filename.rsplit(".", 1)[0] if "." in filename else filename

    # Essaie chaque pattern dans l'ordre de priorité. Le premier qui
    # produit une datetime valide gagne.
    for pattern in (_ISO_DATETIME_RE, _EU_DATETIME_RE, _COMPACT_DATETIME_RE):
        m = pattern.search(base)
        if not m:
            continue
        try:
            year = int(m.group("year"))
            month = int(m.group("month"))
            day = int(m.group("day"))
            hour = int(m.group("hour") or 0)
            minute = int(m.group("minute") or 0)
            second = int(m.group("second") or 0)
        except (TypeError, ValueError):
            continue
        dt = _build_dt(year, month, day, hour, minute, second, tz=tz)
        if dt is not None and _validate(dt):
            return dt
    return None
