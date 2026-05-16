"""Recurrence helpers — Lot 6 (calcul next_occurrence via python-dateutil).

Convertit la `Preparation.recurrence_rule` (dict JSONB) en une règle
``dateutil.rrule`` et calcule la prochaine occurrence à partir d'une date
de référence (généralement ``target_meeting_date`` de la préparation ou
``datetime.now()`` au défaut).

Structure attendue du dict ``rule`` :

    {
        "freq":      "DAILY" | "WEEKLY" | "MONTHLY",
        "interval":  int >= 1 (défaut 1),
        "byweekday": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]   # hebdo
        "byhour":    int 0..23 (optionnel)
        "byminute":  int 0..59 (optionnel)
        "until":     "YYYY-MM-DD" (optionnel, date inclusive)
    }

Toutes les clés sont optionnelles sauf ``freq``. Les valeurs invalides
sont silencieusement ignorées (le helper est best-effort : on préfère ne
pas calculer next_occurrence qu'échouer la requête HTTP qui contient
``recurrence_rule``).
"""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from typing import Any, Optional

try:
    from dateutil.rrule import (
        rrule,
        DAILY, WEEKLY, MONTHLY,
        MO, TU, WE, TH, FR, SA, SU,
    )
except Exception:  # pragma: no cover — dépendance manquante en test ?
    rrule = None
    DAILY = WEEKLY = MONTHLY = None
    MO = TU = WE = TH = FR = SA = SU = None


_FREQ_MAP = {
    "DAILY": DAILY,
    "WEEKLY": WEEKLY,
    "MONTHLY": MONTHLY,
}

_WEEKDAY_MAP = {
    "MO": MO, "TU": TU, "WE": WE, "TH": TH,
    "FR": FR, "SA": SA, "SU": SU,
}


def _coerce_dtstart(value: Any) -> datetime:
    """Retourne un datetime aware UTC pour servir de dtstart à rrule."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _coerce_until(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        u = value
    elif isinstance(value, date):
        u = datetime(value.year, value.month, value.day, 23, 59, 59)
    elif isinstance(value, str):
        try:
            d = date.fromisoformat(value[:10])
            u = datetime(d.year, d.month, d.day, 23, 59, 59)
        except Exception:
            return None
    else:
        return None
    if u.tzinfo is None:
        u = u.replace(tzinfo=timezone.utc)
    return u


def normalize_rule(rule: Any) -> Optional[dict]:
    """Nettoie/normalise un dict recurrence_rule reçu de l'extérieur.

    Retourne ``None`` si ``rule`` n'est pas exploitable. Les clés non
    reconnues sont supprimées ; les types sont coercés.
    """
    if not isinstance(rule, dict):
        return None
    freq = str(rule.get("freq") or "").upper().strip()
    if freq not in _FREQ_MAP:
        return None
    out: dict = {"freq": freq}
    try:
        interval = int(rule.get("interval") or 1)
        if interval < 1:
            interval = 1
        if interval > 365:
            interval = 365
    except (TypeError, ValueError):
        interval = 1
    out["interval"] = interval

    byweekday = rule.get("byweekday")
    if isinstance(byweekday, list):
        codes = [str(x).upper().strip()[:2] for x in byweekday if str(x).strip()]
        codes = [c for c in codes if c in _WEEKDAY_MAP]
        # Dédoublonne en conservant l'ordre.
        seen: set = set()
        ordered: list = []
        for c in codes:
            if c not in seen:
                seen.add(c)
                ordered.append(c)
        if ordered:
            out["byweekday"] = ordered

    for k, lo, hi in (("byhour", 0, 23), ("byminute", 0, 59)):
        v = rule.get(k)
        if v is None or v == "":
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if lo <= iv <= hi:
            out[k] = iv

    until = rule.get("until")
    if until:
        # Conserve la forme string si elle a la forme YYYY-MM-DD pour ne
        # pas perdre d'info après aller-retour JSONB.
        if isinstance(until, str):
            try:
                date.fromisoformat(until[:10])
                out["until"] = until[:10]
            except Exception:
                pass
        elif isinstance(until, date):
            out["until"] = until.isoformat()
    return out


def compute_next_occurrence(
    rule: Any,
    reference: Any = None,
    *,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Calcule la prochaine occurrence postérieure à ``now``.

    ``rule`` : dict normalisé (cf. ``normalize_rule``) ou brut.
    ``reference`` : datetime/date servant de dtstart (généralement
    ``preparation.target_meeting_date``). Si absent, fallback ``now``.
    ``now`` : référence "maintenant" — utile pour tests deterministes.

    Retourne un datetime UTC aware ou ``None`` si non calculable.
    """
    if rrule is None:
        return None
    norm = normalize_rule(rule)
    if not norm:
        return None
    freq = _FREQ_MAP[norm["freq"]]
    dtstart = _coerce_dtstart(reference if reference is not None else now)
    # Si byhour/byminute fournis, on cale dtstart sur cette heure.
    if "byhour" in norm or "byminute" in norm:
        hh = norm.get("byhour", dtstart.hour)
        mm = norm.get("byminute", dtstart.minute)
        dtstart = dtstart.replace(hour=hh, minute=mm, second=0, microsecond=0)

    kwargs: dict = {
        "freq": freq,
        "interval": norm["interval"],
        "dtstart": dtstart,
    }
    if "byweekday" in norm:
        kwargs["byweekday"] = [_WEEKDAY_MAP[c] for c in norm["byweekday"]]
    if "byhour" in norm:
        kwargs["byhour"] = norm["byhour"]
    if "byminute" in norm:
        kwargs["byminute"] = norm["byminute"]
    until = _coerce_until(norm.get("until"))
    if until:
        kwargs["until"] = until

    try:
        r = rrule(**kwargs)
    except Exception:
        return None

    # Cherche la prochaine occurrence strictement postérieure à `now`
    # (ou au défaut datetime.now(utc) + 1 minute pour éviter de retomber
    # sur dtstart lui-même lorsqu'il est dans le passé).
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    threshold = now + timedelta(seconds=1)
    try:
        nxt = r.after(threshold, inc=False)
    except Exception:
        nxt = None
    if nxt is None:
        return None
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return nxt
