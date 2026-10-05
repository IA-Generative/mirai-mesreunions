"""Recherche plein texte dans les réunions d'un utilisateur (contrat de recherche MirAI).

Moteur de ``POST /api/v1/audio/search`` (puller.py), appelé par
``GET /api/v1/search`` de mesreunions-web pour Mon portail.

Recherche seulement : Postgres plein texte (configuration ``french_unaccent``,
migration 024), aucun appel à un modèle de langage. ``q`` est un texte à
chercher, jamais une consigne.

Deux temps :

1. Sélection : les réunions de ``user_sub`` qui correspondent (colonne
   ``search_tsv`` indexée), filtrées par date et par la visibilité ci-dessous,
   classées par ``ts_rank`` sur la colonne stockée. ``total`` est compté APRÈS
   les filtres, avant la limite, et plafonné à ``TOTAL_CAP`` : au-delà,
   ``total_is_lower_bound`` est vrai et ``total`` vaut le plafond.
2. Passages : les blocs d'intervention (``speaker_tagged_text``, hors blocs
   masqués), les points clés et le titre des réunions retenues sont surlignés
   par ``ts_headline`` (même racinisation que la sélection) ; le découpage en
   extraits et le calcul des positions sont faits ici.

Visibilité : FERMÉE PAR DÉFAUT. Une ligne ``user_audio_files`` n'est jamais
supprimée (ni par la purge de la corbeille, ni par « Supprimer
définitivement ») : une règle d'exclusion laisserait réapparaître tout ce qui
a quitté la corbeille. On ne garde donc que ce qui est vivant :

- Upload (web, mobile) : la zone externe fait foi. mesreunions-web envoie la
  liste des uploads VIVANTS de l'utilisateur (ni le fichier ni sa session à la
  corbeille, ligne ``uploaded_files`` présente), appariés comme
  ``/api/v1/audio/lookup`` : ``original_session_code`` + suffixe
  ``/<transcoded_filename>`` de ``stored_filename``. Absent de la liste = non
  renvoyé.
- Import (YouTube, MCR) : renvoyé seulement si au moins une réunion VIVANTE
  (``meetings.trashed_at IS NULL``) lui est liée, par
  ``user_audio_files.meeting_id`` OU ``meetings.user_audio_file_id``. Un
  ré-import après suppression réutilise la ligne existante et pose
  ``user_audio_file_id`` sur la NOUVELLE réunion ; ``meeting_id`` n'est
  réécrit que s'il était nul, il pointe donc encore l'ancienne réunion à la
  corbeille. La règle couvre ce cas comme celui d'une réunion purgée.

Ni ``q`` ni les contenus ne sont journalisés.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import text

MAX_LIMIT = 50
MAX_QUERY_CHARS = 1000
MAX_HITS = 3
SNIPPET_MAX_CHARS = 300
# Contexte gardé avant le premier terme surligné d'un extrait.
_SNIPPET_LEAD_CHARS = 80
# Borne la liste des uploads vivants reçue de mesreunions-web.
MAX_UPLOADS = 20000
# Au-delà, ``total`` vaut TOTAL_CAP et ``total_is_lower_bound`` est vrai :
# compter toutes les correspondances d'une requête très large ne sert à rien.
TOTAL_CAP = 1000
# Taille maximale d'un texte passé à ts_headline (HighlightAll est linéaire).
HEADLINE_MAX_CHARS = 50000
# Une requête de recherche ne doit jamais monopoliser une connexion.
STATEMENT_TIMEOUT_MS = 5000
TS_CONFIG = "french_unaccent"

IMPORT_SOURCE_TYPES = ("youtube_subtitle", "youtube_audio")
IMPORT_ORIGINS = ("mcr_import",)

# Marqueurs de surlignage passés à ts_headline. Caractères de contrôle :
# retirés des textes avant l'envoi, ils ne peuvent pas venir du contenu.
_HL_START = "\x02"
_HL_END = "\x03"
_HEADLINE_OPTS = f"HighlightAll=true, StartSel={_HL_START}, StopSel={_HL_END}"

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_MD_EMPH_RE = re.compile(r"(\*\*|__)")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


class SearchValidationError(ValueError):
    """Paramètre de recherche invalide (→ 400)."""


# ─── Texte ──────────────────────────────────────────────────────────────────

def utf16_len(s: str) -> int:
    """Longueur en unités UTF-16, comme ``String.length`` en JavaScript."""
    return len(s.encode("utf-16-le")) // 2


def clean_text(value: Optional[str]) -> str:
    """Texte brut sur une ligne : sans caractère de contrôle, blancs écrasés."""
    return _WS_RE.sub(" ", _CTRL_RE.sub(" ", value or "")).strip()


def flatten_key_points(value: Optional[str]) -> str:
    """Points clés (liste Markdown) → une ligne de texte brut séparée par « · »."""
    items = []
    for line in (value or "").splitlines():
        line = _MD_EMPH_RE.sub("", _BULLET_RE.sub("", line))
        line = clean_text(line)
        if line:
            items.append(line)
    return " · ".join(items)


def blocks_from_reparsed(reparsed: Iterable[dict]) -> list[dict]:
    """Blocs de ``_reparse_speaker_tagged_blocks`` → ``{speaker, start, end, text}``.

    Le texte d'un bloc est la concaténation de ses lignes ``> …``.
    """
    out = []
    for b in reparsed or []:
        body = " ".join(
            (line or "").strip().lstrip(">").strip() for line in b.get("body_lines") or []
        )
        out.append({
            "speaker": (b.get("speaker") or "").strip() or None,
            "start": b.get("start"),
            "end": b.get("end"),
            "text": clean_text(body),
        })
    return out


def parse_marked(marked: str) -> tuple[str, list[tuple[int, int]]]:
    """Retire les marqueurs de ts_headline ; renvoie le texte et les plages
    surlignées (indices Python, ``[début, fin[``)."""
    plain: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    start = None
    for ch in marked or "":
        if ch == _HL_START:
            start = pos
        elif ch == _HL_END:
            if start is not None and pos > start:
                spans.append((start, pos))
            start = None
        else:
            plain.append(ch)
            pos += 1
    return "".join(plain), spans


def build_snippet(plain: str, spans: list[tuple[int, int]],
                  max_chars: int = SNIPPET_MAX_CHARS) -> tuple[str, list[list[int]]]:
    """Extrait d'au plus ~``max_chars`` caractères autour du premier terme
    surligné, coupé sur des blancs, avec « … » aux bords coupés.

    Les positions renvoyées sont en unités UTF-16 dans l'extrait.
    """
    n = len(plain)
    if n <= max_chars:
        lo, hi = 0, n
    else:
        anchor = spans[0][0] if spans else 0
        lo = max(0, anchor - _SNIPPET_LEAD_CHARS)
        hi = min(n, lo + max_chars)
        lo = max(0, hi - max_chars)
        # Coupe sur un blanc, sans jamais couper un terme surligné.
        if lo > 0:
            cut = plain.find(" ", lo, anchor if spans else hi)
            if cut != -1:
                lo = cut + 1
        if hi < n:
            last_needed = max((e for s, e in spans if s < hi), default=lo)
            cut = plain.rfind(" ", max(lo, last_needed), hi)
            if cut != -1 and cut > lo:
                hi = cut
    body = plain[lo:hi].strip()
    lead_strip = len(plain[lo:hi]) - len(plain[lo:hi].lstrip())
    lo += lead_strip
    prefix = "…" if lo > 0 else ""
    suffix = "…" if lo + len(body) < n else ""
    snippet = prefix + body + suffix
    highlights: list[list[int]] = []
    for s, e in spans:
        if s < lo or e > lo + len(body):
            continue
        a = len(prefix) + (s - lo)
        b = len(prefix) + (e - lo)
        highlights.append([utf16_len(snippet[:a]), utf16_len(snippet[:b])])
    return snippet, highlights


# ─── Paramètres ─────────────────────────────────────────────────────────────

def _parse_dt(value, name: str) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SearchValidationError(f"{name} illisible") from exc
    if dt.tzinfo is None:
        raise SearchValidationError(f"{name} doit porter un fuseau")
    return dt


def validate_payload(data) -> dict:
    """Valide le corps de ``POST /api/v1/audio/search``.

    mesreunions-web a déjà validé ``q`` et ``limit`` côté contrat ; on
    revalide ici (défense en profondeur, appel interne). ``uploads`` est
    obligatoire : sans liste, aucun upload ne sort (fermé par défaut).
    """
    if not isinstance(data, dict):
        raise SearchValidationError("corps JSON attendu")
    user_sub = str(data.get("user_sub") or "").strip()
    if not user_sub:
        raise SearchValidationError("user_sub requis")
    q = str(data.get("q") or "").strip()
    if not q or len(q) > MAX_QUERY_CHARS:
        raise SearchValidationError("q : 1 à 1000 caractères")
    try:
        limit = int(data.get("limit", 20))
    except (TypeError, ValueError) as exc:
        raise SearchValidationError("limit entier attendu") from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise SearchValidationError("limit : 1 à 50")
    date_from = _parse_dt(data.get("from"), "from")
    date_to = _parse_dt(data.get("to"), "to")
    raw = data.get("uploads")
    if not isinstance(raw, list) or len(raw) > MAX_UPLOADS:
        raise SearchValidationError("uploads : liste attendue")
    up_codes, up_files = [], []
    for it in raw:
        if not isinstance(it, dict):
            raise SearchValidationError("uploads : objets {simple_code, filename}")
        code = str(it.get("simple_code") or "").strip()
        fname = str(it.get("filename") or "").strip()
        if code and fname:
            up_codes.append(code)
            up_files.append(fname)
    return {
        "user_sub": user_sub, "q": q, "limit": limit,
        "date_from": date_from, "date_to": date_to,
        "up_codes": up_codes, "up_files": up_files,
    }


# ─── SQL ────────────────────────────────────────────────────────────────────

_IS_IMPORT = (
    "(u.source_type::text IN ('youtube_subtitle', 'youtube_audio') "
    "OR u.origin = 'mcr_import')"
)

# ``matched`` : ids seulement (aucun texte lu). Le classement ne lit que la
# colonne stockée ``search_tsv`` des réunions retenues par les filtres.
_SELECT_SQL = f"""
WITH qq AS (SELECT websearch_to_tsquery('{TS_CONFIG}', :q) AS q),
live AS (
    SELECT l.code, l.fname
      FROM unnest(CAST(:up_codes AS text[]), CAST(:up_files AS text[])) AS l(code, fname)
),
cand AS (
    SELECT u.id
      FROM user_audio_files u, qq
     WHERE u.user_sub = :user_sub
       AND u.search_tsv @@ qq.q
    UNION
    SELECT u.id
      FROM user_audio_files u
      JOIN video_sources vs ON vs.id = u.external_video_source_id, qq
     WHERE u.user_sub = :user_sub
       AND to_tsvector('{TS_CONFIG}', coalesce(vs.title, '')) @@ qq.q
),
matched AS MATERIALIZED (
    SELECT u.id
      FROM cand
      JOIN user_audio_files u ON u.id = cand.id
     WHERE u.user_sub = :user_sub
       AND (CAST(:date_from AS timestamptz) IS NULL
            OR coalesce(u.meeting_datetime, u.created_at) >= CAST(:date_from AS timestamptz))
       AND (CAST(:date_to AS timestamptz) IS NULL
            OR coalesce(u.meeting_datetime, u.created_at) <= CAST(:date_to AS timestamptz))
       AND CASE WHEN {_IS_IMPORT} THEN
                -- Import : au moins une réunion liée vivante.
                EXISTS (SELECT 1 FROM meetings m
                         WHERE m.user_sub = u.user_sub
                           AND (m.id = u.meeting_id OR m.user_audio_file_id = u.id)
                           AND m.trashed_at IS NULL)
            ELSE
                -- Upload : présent dans la liste des uploads vivants.
                EXISTS (SELECT 1 FROM live
                         WHERE live.code = u.original_session_code
                           AND (u.stored_filename = live.fname
                                OR right(u.stored_filename, length(live.fname) + 1)
                                   = '/' || live.fname))
            END
)
SELECT u.id, u.suggested_filename, u.original_filename, vs.title AS vs_title,
       u.meeting_datetime, u.created_at, u.source_type::text AS source_type,
       u.origin, u.audio_duration_seconds, vs.duration_sec,
       u.key_points_summary, u.speaker_tagged_text, u.meeting_analysis_json,
       u.hidden_block_indices,
       ts_rank(coalesce(u.search_tsv, ''::tsvector)
               || setweight(to_tsvector('{TS_CONFIG}', coalesce(vs.title, '')), 'A'),
               qq.q) AS score,
       (SELECT count(*) FROM (SELECT 1 FROM matched LIMIT :total_probe) c) AS total
  FROM matched
  JOIN user_audio_files u ON u.id = matched.id
  LEFT JOIN video_sources vs ON vs.id = u.external_video_source_id
 CROSS JOIN qq
 ORDER BY score DESC, coalesce(u.meeting_datetime, u.created_at) DESC, u.id
 LIMIT :limit
"""

# Passages : ne surligne que les textes qui partagent au moins un lexème avec
# la requête (filtre peu coûteux avant ts_headline).
_HEADLINE_SQL = f"""
WITH qq AS (SELECT websearch_to_tsquery('{TS_CONFIG}', :q) AS q),
     ql AS (SELECT tsvector_to_array(to_tsvector('{TS_CONFIG}', :q)) AS lex)
SELECT b.k, ts_headline('{TS_CONFIG}', b.t, qq.q, :opts) AS marked
  FROM unnest(CAST(:keys AS int[]), CAST(:texts AS text[])) AS b(k, t), qq, ql
 WHERE tsvector_to_array(to_tsvector('{TS_CONFIG}', b.t)) && ql.lex
"""

# Repli : texte intégral des réunions SANS bloc d'intervention lisible (pas de
# diarisation). Une réunion qui a des blocs n'y passe jamais : sa
# correspondance, si aucun bloc visible ne la porte, est dans un bloc masqué.
_FULLTEXT_SQL = """
SELECT u.id, left(coalesce(u.speaker_tagged_text, u.cleaned_text, u.transcription_text, ''),
                  :max_chars) AS body
  FROM user_audio_files u
 WHERE u.user_sub = :user_sub
   AND u.id = ANY(CAST(:ids AS uuid[]))
"""
_SPEAKER_HEADER_RE = re.compile(r"^[ \t]*\*\*[^*]+\*\*[ \t]*_\([^)]*\)_[ \t]*$", re.MULTILINE)
_QUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)


def flatten_transcript(value: Optional[str]) -> str:
    """Transcription brute ou balisée → texte brut sur une ligne (en-têtes de
    blocs et marques de citation retirés)."""
    value = _SPEAKER_HEADER_RE.sub(" ", value or "")
    value = _QUOTE_RE.sub("", value)
    return clean_text(_MD_EMPH_RE.sub("", value))


def _headlines(db, q: str, texts: list[str]) -> dict[int, str]:
    """``ts_headline`` des textes qui partagent un lexème avec la requête."""
    if not texts:
        return {}
    rows = db.execute(text(_HEADLINE_SQL), {
        "q": q,
        "opts": _HEADLINE_OPTS,
        "keys": list(range(len(texts))),
        "texts": [t[:HEADLINE_MAX_CHARS] for t in texts],
    }).all()
    return {int(k): m for k, m in rows}


# ─── Contexte d'une réunion ─────────────────────────────────────────────────

def participants(analysis_raw, blocks: list[dict], source_type: str):
    """(nombre, base) des participants.

    ``participants_presents`` de l'analyse (personnes qui ont parlé) ; à
    défaut, locuteurs distincts des blocs ; sinon nul. Jamais ``actors`` :
    c'est l'union des présents et des personnes simplement citées. Les
    sous-titres YouTube n'ont qu'un locuteur fictif → pas de comptage.
    """
    try:
        obj = json.loads(analysis_raw) if analysis_raw else None
    except (TypeError, ValueError):
        obj = None
    presents = obj.get("participants_presents") if isinstance(obj, dict) else None
    if isinstance(presents, list) and presents:
        return len(presents), "analysis"
    if source_type == "youtube_subtitle":
        return None, None
    speakers = {b["speaker"] for b in blocks if b.get("speaker")}
    if speakers:
        return len(speakers), "speakers"
    return None, None


def date_and_kind(meeting_dt, created_at, is_import: bool):
    """Date du résultat et sa nature : ``meeting`` (date de réunion saisie),
    sinon date de création de la ligne, ``import`` ou ``upload``."""
    if meeting_dt is not None:
        return meeting_dt.isoformat(), "meeting"
    if created_at is not None:
        return created_at.isoformat(), ("import" if is_import else "upload")
    return None, None


def _as_seconds(value):
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


# ─── Recherche ──────────────────────────────────────────────────────────────

_FIELD_ORDER = {"transcript": 0, "key_points": 1, "title": 2}


def run_search(db, params: dict, *, reparse_blocks) -> dict:
    """Exécute la recherche. ``reparse_blocks`` = parseur des blocs
    ``speaker_tagged_text`` du puller (réutilisé, pas recopié).

    Renvoie ``{"total": int, "total_is_lower_bound": bool, "results": [...]}`` ; chaque résultat porte
    ``uaf_id`` et les champs du contrat hors URL (construites par
    mesreunions-web, qui connaît l'URL publique).
    """
    db.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))
    rows = db.execute(text(_SELECT_SQL), {
        "q": params["q"],
        "user_sub": params["user_sub"],
        "limit": params["limit"],
        "date_from": params["date_from"],
        "date_to": params["date_to"],
        "up_codes": params["up_codes"],
        "up_files": params["up_files"],
        # Un de plus que le plafond : distingue « exactement TOTAL_CAP » de
        # « au moins TOTAL_CAP ».
        "total_probe": TOTAL_CAP + 1,
    }).mappings().all()
    if not rows:
        return {"total": 0, "total_is_lower_bound": False, "results": []}
    total = int(rows[0]["total"])
    total_is_lower_bound = total > TOTAL_CAP
    total = min(total, TOTAL_CAP)

    # Textes candidats aux passages : (résultat, champ, bloc) → texte.
    keys: list[tuple[int, str, Optional[int]]] = []
    texts: list[str] = []
    per_result_blocks: list[list[dict]] = []
    for ri, r in enumerate(rows):
        blocks = blocks_from_reparsed(reparse_blocks(r["speaker_tagged_text"] or ""))
        per_result_blocks.append(blocks)
        hidden = set(r["hidden_block_indices"] or [])
        for bi, b in enumerate(blocks):
            if bi in hidden or not b["text"]:
                continue
            keys.append((ri, "transcript", bi))
            texts.append(b["text"])
        kp = flatten_key_points(r["key_points_summary"])
        if kp:
            keys.append((ri, "key_points", None))
            texts.append(kp)
        title = clean_text(r["suggested_filename"] or r["vs_title"] or r["original_filename"])
        if title:
            keys.append((ri, "title", None))
            texts.append(title)
        # Titre d'origine d'un import, s'il diffère : c'est souvent lui qui
        # porte le nom de l'intervenant.
        vs_title = clean_text(r["vs_title"])
        if vs_title and vs_title != title:
            keys.append((ri, "title", -1))
            texts.append(vs_title)

    marked_by_key = _headlines(db, params["q"], texts)

    # Réunions sans bloc lisible : on cherche dans le texte intégral (passage
    # sans position).
    no_blocks = [ri for ri in range(len(rows)) if not per_result_blocks[ri]]
    if no_blocks:
        bodies = db.execute(text(_FULLTEXT_SQL), {
            "user_sub": params["user_sub"],
            "ids": [str(rows[ri]["id"]) for ri in no_blocks],
            "max_chars": HEADLINE_MAX_CHARS,
        }).all()
        index_by_id = {str(rows[ri]["id"]): ri for ri in no_blocks}
        extra_keys, extra_texts = [], []
        for uid, body in bodies:
            flat = flatten_transcript(body)
            if flat:
                extra_keys.append((index_by_id[str(uid)], "transcript", None))
                extra_texts.append(flat)
        for k, marked in _headlines(db, params["q"], extra_texts).items():
            keys.append(extra_keys[k])
            marked_by_key[len(keys) - 1] = marked

    # Candidats par résultat, triés : plus de termes distincts d'abord, puis
    # transcription > points clés > titre, puis ordre chronologique.
    candidates: list[list[tuple]] = [[] for _ in rows]
    for k, marked in marked_by_key.items():
        ri, field, bi = keys[k]
        plain, spans = parse_marked(marked)
        if not spans:
            continue
        distinct = len({plain[s:e].lower() for s, e in spans})
        candidates[ri].append((-distinct, _FIELD_ORDER[field], bi if bi is not None else 0,
                               field, bi, plain, spans))

    results = []
    for ri, r in enumerate(rows):
        blocks = per_result_blocks[ri]
        source_type = r["source_type"] or "upload"
        is_import = source_type in IMPORT_SOURCE_TYPES or r["origin"] in IMPORT_ORIGINS
        hits = []
        for cand in sorted(candidates[ri], key=lambda c: c[:3])[:MAX_HITS]:
            _d, _o, _p, field, bi, plain, spans = cand
            snippet, highlights = build_snippet(plain, spans)
            location = None
            speaker = None
            if field == "transcript" and bi is not None:
                b = blocks[bi]
                location = {
                    "start_seconds": _as_seconds(b["start"]),
                    "end_seconds": _as_seconds(b["end"]),
                    "page": None,
                }
                # Les sous-titres YouTube n'ont qu'un locuteur fictif.
                if source_type != "youtube_subtitle":
                    speaker = b["speaker"]
            hits.append({
                "snippet": snippet,
                "highlights": highlights,
                "field": field,
                "location": location,
                "speaker": speaker,
            })
        date, date_kind = date_and_kind(r["meeting_datetime"], r["created_at"], is_import)
        participants_count, participants_basis = participants(
            r["meeting_analysis_json"], blocks, source_type)
        score = r["score"]
        results.append({
            "uaf_id": str(r["id"]),
            "title": clean_text(r["suggested_filename"] or r["vs_title"]
                                or r["original_filename"]) or "Réunion",
            "date": date,
            "score": round(float(score), 4) if score is not None else None,
            "hits": hits,
            "context": {
                "duration_seconds": _as_seconds(
                    r["audio_duration_seconds"]
                    if r["audio_duration_seconds"] is not None else r["duration_sec"]),
                "participants_count": participants_count,
                "participants_basis": participants_basis,
                "source_type": source_type,
                "date_kind": date_kind,
            },
        })
    return {"total": total, "total_is_lower_bound": total_is_lower_bound, "results": results}
