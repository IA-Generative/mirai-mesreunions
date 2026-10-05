"""Blueprint ``search`` — ``GET /api/v1/search`` (contrat de recherche MirAI).

Appelée depuis le navigateur par Mon portail (MySearch, autre origine) avec
le jeton de l'utilisateur. Recherche seulement : aucun appel à un modèle de
langage, ``q`` est un texte à chercher, jamais une consigne.

Sécurité :
- ``Authorization: Bearer`` vérifié par ``verify_oidc_token`` (JWKS, ``iss``,
  ``exp``) ; l'audience de MesRéunions doit figurer dans ``aud`` et ``azp``
  dans la liste autorisée. Jamais ``audience=mysearch`` (confusion
  d'audience). Aucune session cookie : la route n'est pas derrière
  ``require_auth``.
- ``user_sub`` = ``sub`` du jeton, jamais un paramètre.
- CORS limité à ce blueprint, origines lues dans l'environnement, sans
  ``Allow-Credentials``.
- ``Cache-Control: no-store`` sur toutes les réponses ; ni ``q`` ni les
  contenus ne sont journalisés.

Variables d'environnement :
  MESREUNIONS_CORS_ORIGINS        origines autorisées, séparées par des virgules
                                  (vide = aucun appel navigateur cross-origin)
  SEARCH_OIDC_AUDIENCE            audience attendue dans ``aud`` (défaut mes-reunions)
  SEARCH_ALLOWED_AZP              clients autorisés, virgules (défaut mysearch)
  SEARCH_OIDC_ISSUER              émetteur(s) accepté(s), virgules
                                  (défaut OIDC_ISSUER + OIDC_INTERNAL_ISSUER)
  SEARCH_OIDC_JWKS_URL            JWKS (défaut <émetteur interne>/protocol/openid-connect/certs)
  SEARCH_RATE_LIMIT_PER_MINUTE    requêtes par utilisateur et par minute (défaut 30),
                                  comptées PAR PROCESSUS : limite effective
                                  = valeur × workers gunicorn × réplicas
  SEARCH_INGESTER_TIMEOUT_SECONDS délai d'appel à internal-ingester (défaut 7)
  PUBLIC_BASE_URL                 base publique https des liens renvoyés
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dtime

import requests as req
from flask import Blueprint, jsonify, make_response, request

import app.shared  # noqa: F401  — insère le repo root dans sys.path
from app.runtime import session_scope
from libs.shared.app.config import INTERNAL_API_TOKEN
from libs.shared.app.models import UploadSession, UploadedFile
from libs.shared.app.oidc_auth import (
    OidcAudienceError, OidcAuthError, is_production, verify_oidc_token,
)
from libs.shared.app.rate_limit import SlidingWindowLimiter

logger = logging.getLogger("mesreunions_web.search")

bp = Blueprint("search", __name__)

SOURCE_ID = "mesreunions"
MAX_QUERY_CHARS = 1000
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
_RATE_WINDOW_SECONDS = 60
_PARIS_TZ_NAME = "Europe/Paris"

_limiter = SlidingWindowLimiter(
    max_events=int(os.getenv("SEARCH_RATE_LIMIT_PER_MINUTE", "30")),
    window_seconds=_RATE_WINDOW_SECONDS,
)


class _SearchError(Exception):
    def __init__(self, status: int, code: str, message: str, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def _error(status: int, code: str, message: str, headers=None):
    resp = jsonify({"error": {"code": code, "message": message}})
    resp.status_code = status
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def _csv_env(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in (os.getenv(name, default) or "").split(",") if v.strip()]


# ─── CORS (ce blueprint uniquement) ─────────────────────────────────────────

def _allowed_origins() -> set[str]:
    return {o.rstrip("/") for o in _csv_env("MESREUNIONS_CORS_ORIGINS")}


def _origin_allowed(origin: str | None) -> bool:
    return bool(origin) and origin.rstrip("/") in _allowed_origins()


@bp.after_request
def _contract_headers(resp):
    # Toutes les réponses du contrat, erreurs comprises : jamais de cache, et
    # l'origine autorisée lit aussi les erreurs (sinon le navigateur les
    # masque et Mon portail ne sait pas dire « jeton expiré »).
    resp.headers["Cache-Control"] = "no-store"
    resp.vary.add("Origin")
    origin = request.headers.get("Origin")
    if _origin_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Expose-Headers"] = "Retry-After"
    return resp


# ─── Jeton ──────────────────────────────────────────────────────────────────

def _oidc_settings() -> tuple[str, set[str], list[str], str]:
    audience = os.getenv("SEARCH_OIDC_AUDIENCE", "mes-reunions").strip()
    azp_allowed = set(_csv_env("SEARCH_ALLOWED_AZP", "mysearch"))
    issuers = [i.rstrip("/") for i in _csv_env("SEARCH_OIDC_ISSUER")]
    if not issuers:
        issuers = [i.rstrip("/") for i in (os.getenv("OIDC_ISSUER", ""),
                                            os.getenv("OIDC_INTERNAL_ISSUER", "")) if i]
    jwks_url = os.getenv("SEARCH_OIDC_JWKS_URL", "").strip()
    if not jwks_url:
        base = (os.getenv("OIDC_INTERNAL_ISSUER") or os.getenv("OIDC_ISSUER") or "").rstrip("/")
        jwks_url = f"{base}/protocol/openid-connect/certs" if base else ""
    return audience, azp_allowed, issuers, jwks_url


def _authenticate() -> dict:
    """Vérifie le Bearer et renvoie ses claims. Fail-closed."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or not header[7:].strip():
        raise _SearchError(401, "invalid_token", "Jeton absent.")
    token = header[7:].strip()
    audience, azp_allowed, issuers, jwks_url = _oidc_settings()
    if not audience or not issuers or not jwks_url or not azp_allowed:
        logger.error("search: configuration OIDC incomplète (audience/émetteur/JWKS/azp)")
        raise _SearchError(503, "search_unavailable", "Recherche indisponible.")
    try:
        claims = verify_oidc_token(token, audience=audience, issuer=set(issuers),
                                   jwks_url=jwks_url)
    except OidcAudienceError:
        raise _SearchError(403, "audience_mismatch", "Jeton destiné à un autre service.")
    except OidcAuthError as exc:
        if exc.status >= 500:
            logger.error("search: vérification du jeton impossible (configuration)")
            raise _SearchError(503, "search_unavailable", "Recherche indisponible.")
        raise _SearchError(401, "invalid_token", "Jeton invalide ou expiré.")
    except Exception as exc:
        # JWKS injoignable : le service ne peut pas vérifier → indisponible.
        logger.error("search: JWKS indisponible (%s)", type(exc).__name__)
        raise _SearchError(503, "search_unavailable", "Recherche indisponible.")
    # Le contrat exige l'audience DANS ``aud`` (verify_oidc_token accepte
    # aussi azp == audience, cas d'un jeton émis pour MesRéunions elle-même)
    # et un client émetteur autorisé.
    aud = claims.get("aud")
    aud = [aud] if isinstance(aud, str) else (aud or [])
    if audience not in aud or claims.get("azp") not in azp_allowed:
        raise _SearchError(403, "audience_mismatch", "Jeton destiné à un autre service.")
    return claims


# ─── Paramètres ─────────────────────────────────────────────────────────────

def _paris_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(_PARIS_TZ_NAME)
    except Exception:  # base tz absente de l'image : repli UTC
        from datetime import timezone
        return timezone.utc


def _parse_bound(raw: str | None, *, end: bool):
    """``AAAA-MM-JJ`` ou datetime ISO 8601 → datetime avec fuseau.

    Bornes incluses : une date seule couvre toute la journée (heure de
    Paris). Un datetime sans fuseau est lu à l'heure de Paris.
    """
    if raw is None or raw.strip() == "":
        return None
    value = raw.strip()
    try:
        if len(value) == 10:
            d = datetime.strptime(value, "%Y-%m-%d").date()
            return datetime.combine(d, dtime.max if end else dtime.min, tzinfo=_paris_tz())
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _SearchError(400, "invalid_query", "Date illisible (AAAA-MM-JJ ou ISO 8601).")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_paris_tz())
    return dt


def _parse_params() -> dict:
    q = (request.args.get("q") or "").strip()
    if not q:
        raise _SearchError(400, "invalid_query", "Le paramètre q est obligatoire.")
    if len(q) > MAX_QUERY_CHARS:
        raise _SearchError(400, "invalid_query", "q dépasse 1000 caractères.")
    raw_limit = request.args.get("limit")
    if raw_limit in (None, ""):
        limit = DEFAULT_LIMIT
    else:
        try:
            limit = int(raw_limit)
        except ValueError:
            raise _SearchError(400, "invalid_query", "limit doit être un entier.")
        if not 1 <= limit <= MAX_LIMIT:
            raise _SearchError(400, "invalid_query", "limit doit être compris entre 1 et 50.")
    date_from = _parse_bound(request.args.get("from"), end=False)
    date_to = _parse_bound(request.args.get("to"), end=True)
    if date_from and date_to and date_from > date_to:
        raise _SearchError(400, "invalid_query", "from est postérieur à to.")
    # ``scope`` : MesRéunions n'a pas de notion de périmètre, paramètre ignoré.
    return {"q": q, "limit": limit, "from": date_from, "to": date_to}


# ─── Uploads vivants (zone externe) ─────────────────────────────────────────

def _live_uploads(user_sub: str) -> list[dict]:
    """Uploads VIVANTS de l'utilisateur : ni le fichier ni sa session à la
    corbeille — mêmes critères que ``/api/my-sessions`` (sans son plafond
    d'affichage). Appariés côté ingester comme ``/api/v1/audio/lookup`` (code
    de session + nom transcodé).

    Fermé par défaut : un upload purgé ou supprimé définitivement n'a plus de
    ligne ici, il ne peut donc pas ressortir, alors que sa ligne
    ``user_audio_files`` subsiste en zone interne.
    """
    db = session_scope()
    try:
        rows = (
            db.query(UploadSession.simple_code, UploadedFile.transcoded_filename)
            .join(UploadedFile, UploadedFile.session_id == UploadSession.id)
            .filter(
                UploadSession.user_sub == user_sub,
                UploadSession.trashed_at.is_(None),
                UploadedFile.trashed_at.is_(None),
                UploadedFile.transcoded_filename.isnot(None),
            )
            .all()
        )
        return [{"simple_code": code, "filename": fname} for code, fname in rows if code and fname]
    finally:
        db.close()


# ─── Appel internal-ingester ────────────────────────────────────────────────

def _ingester_base() -> str:
    return (os.getenv("FILE_PULLER_INTERNAL_BASE_URL")
            or "http://internal-ingester:8090").rstrip("/")


def _call_ingester(payload: dict) -> dict:
    timeout = float(os.getenv("SEARCH_INGESTER_TIMEOUT_SECONDS", "7"))
    try:
        resp = req.post(
            f"{_ingester_base()}/api/v1/audio/search",
            json=payload,
            headers={"Authorization": f"Bearer {INTERNAL_API_TOKEN}"},
            timeout=timeout,
        )
    except req.RequestException as exc:
        logger.warning("search: internal-ingester injoignable (%s)", type(exc).__name__)
        raise _SearchError(503, "search_unavailable", "Recherche indisponible.")
    if resp.status_code == 400:
        raise _SearchError(400, "invalid_query", "Requête refusée.")
    if resp.status_code != 200:
        logger.warning("search: internal-ingester HTTP %s", resp.status_code)
        raise _SearchError(503, "search_unavailable", "Recherche indisponible.")
    try:
        body = resp.json()
    except ValueError:
        raise _SearchError(503, "search_unavailable", "Recherche indisponible.")
    if not isinstance(body, dict):
        raise _SearchError(503, "search_unavailable", "Recherche indisponible.")
    return body


# ─── Réponse ────────────────────────────────────────────────────────────────

def _public_base() -> str:
    base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base
    base = request.host_url.rstrip("/")
    if is_production() and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


def _to_contract(body: dict, q: str) -> dict:
    base = _public_base()
    results = []
    for r in body.get("results") or []:
        uaf_id = str(r.get("uaf_id") or "")
        if not uaf_id:
            continue
        url = f"{base}/reunion/{uaf_id}"
        hits = []
        for h in (r.get("hits") or [])[:3]:
            loc = h.get("location")
            start = loc.get("start_seconds") if isinstance(loc, dict) else None
            hits.append({
                "snippet": h.get("snippet") or "",
                "highlights": h.get("highlights") or [],
                "field": h.get("field"),
                "location": loc,
                "speaker": h.get("speaker"),
                "url": f"{url}?t={int(start)}" if start is not None else url,
            })
        results.append({
            "id": uaf_id,
            "title": r.get("title"),
            "date": r.get("date"),
            "url": url,
            "score": r.get("score"),
            "hits": hits,
            "context": r.get("context") or {},
        })
    total = int(body.get("total") or 0)
    # ``total`` est plafonné côté ingester : au-delà du plafond, il ne vaut
    # qu'une borne basse et Mon portail doit l'afficher comme telle.
    lower_bound = bool(body.get("total_is_lower_bound"))
    return {
        "source": SOURCE_ID,
        "query": q,
        "total": total,
        "total_is_lower_bound": lower_bound,
        "truncated": lower_bound or total > len(results),
        "results": results,
    }


# ─── Routes ─────────────────────────────────────────────────────────────────

@bp.route("/api/v1/search", methods=["OPTIONS"])
def search_preflight():
    if not _origin_allowed(request.headers.get("Origin")):
        return _error(403, "forbidden", "Origine non autorisée.")
    out = make_response("", 204)
    out.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    out.headers["Access-Control-Allow-Headers"] = "Authorization"
    out.headers["Access-Control-Max-Age"] = "600"
    return out


@bp.route("/api/v1/search", methods=["GET"])
def search():
    t0 = time.monotonic()
    try:
        claims = _authenticate()
        user_sub = str(claims.get("sub") or "").strip()
        if not _limiter.allow(user_sub, now=time.monotonic()):
            raise _SearchError(429, "rate_limited", "Trop de recherches, réessayez dans une minute.",
                               headers={"Retry-After": str(_RATE_WINDOW_SECONDS)})
        params = _parse_params()
        try:
            uploads = _live_uploads(user_sub)
        except Exception as exc:
            logger.error("search: lecture des uploads impossible (%s)", type(exc).__name__)
            raise _SearchError(503, "search_unavailable", "Recherche indisponible.")
        body = _call_ingester({
            "user_sub": user_sub,
            "q": params["q"],
            "limit": params["limit"],
            "from": params["from"].isoformat() if params["from"] else None,
            "to": params["to"].isoformat() if params["to"] else None,
            "uploads": uploads,
        })
        out = _to_contract(body, params["q"])
    except _SearchError as exc:
        return _error(exc.status, exc.code, exc.message, exc.headers)
    logger.info("search user=%s q_len=%d limit=%d total=%d returned=%d ms=%d",
                user_sub[:12], len(params["q"]), params["limit"], out["total"],
                len(out["results"]), round((time.monotonic() - t0) * 1000))
    return jsonify(out)
