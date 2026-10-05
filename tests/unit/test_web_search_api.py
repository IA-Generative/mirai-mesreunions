"""Tests de ``GET /api/v1/search`` (contrat de recherche MirAI) et du lien
``/reunion/<id>`` de mesreunions-web.

La route est appelée depuis le navigateur par Mon portail, sur une autre
origine, avec le jeton de l'utilisateur : on vérifie ici le jeton (audience,
client émetteur), le CORS limité, les erreurs du contrat, ``no-store`` et la
mise en forme de la réponse. internal-ingester est simulé (``requests.post``)
et la liste des uploads vivants aussi : leur logique est couverte par
``test_meeting_search.py``.
"""

import importlib.util
import os
import sys
import time
import types
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")
from authlib.jose import JsonWebKey, jwt  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_INTERNAL_TOKEN = "not-a-real-credential-synthetic-fixture-value"
ISSUER = "https://sso.example.test/realms/mirai"
JWKS_URL = "https://sso.example.test/realms/mirai/protocol/openid-connect/certs"
PORTAIL = "https://portail.example.test"
PUBLIC = "https://reunions.example.test"
UAF = "5e5e0000-0000-4000-8000-0000000000a1"


def _load_web():
    """Charge ``main.py`` avec les effets de bord du boot neutralisés."""
    os.environ["INTERNAL_API_TOKEN"] = _INTERNAL_TOKEN
    os.environ.setdefault("SECRET_KEY", "test-secret-key-32-bytes-long-xxxxx")
    os.environ.setdefault("OIDC_ISSUER", "https://kc.test/realms/test")
    os.environ.setdefault("OIDC_CLIENT_ID", "test-client")
    os.environ.setdefault("OIDC_CLIENT_SECRET", "test-secret")
    os.environ.setdefault("OIDC_REDIRECT_URI", "http://test/auth/callback")

    for name in list(sys.modules):
        if name.startswith("libs.shared.app") or name in {"libs.shared", "libs"}:
            stub = sys.modules.get(name)
            if stub is not None and not getattr(stub, "__file__", None):
                sys.modules.pop(name, None)
    for name in ("requests", "authlib", "authlib.integrations",
                 "authlib.integrations.flask_client"):
        stub = sys.modules.get(name)
        if stub is not None and not getattr(stub, "__file__", None):
            sys.modules.pop(name, None)

    web_dir = os.path.join(ROOT, "services", "mesreunions-web")
    while web_dir in sys.path:
        sys.path.remove(web_dir)
    sys.path.insert(0, web_dir)
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        sys.modules.pop(name, None)

    if "pika" not in sys.modules:
        pika = types.ModuleType("pika")
        pika.BlockingConnection = MagicMock()
        pika.ConnectionParameters = MagicMock()
        pika.PlainCredentials = MagicMock()
        pika.exceptions = types.SimpleNamespace(
            AMQPConnectionError=Exception, ChannelClosedByBroker=Exception)
        sys.modules["pika"] = pika
    if "qrcode" not in sys.modules:
        qr = types.ModuleType("qrcode")
        qr.QRCode = MagicMock()
        consts = types.SimpleNamespace(ERROR_CORRECT_M=0)
        qr.constants = consts
        sys.modules["qrcode"] = qr
        sys.modules["qrcode.constants"] = consts

    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = lambda *_a, **_k: MagicMock()
    db_stub.init_tables = MagicMock()
    db_stub.with_db_retry = lambda fn, **_k: fn()
    db_stub.__file__ = "<stub>"
    sys.modules["libs.shared.app.database"] = db_stub

    sys.modules.pop("mesreunions_web_search_api_test", None)
    spec = importlib.util.spec_from_file_location(
        "mesreunions_web_search_api_test",
        os.path.join(web_dir, "app", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── Jetons ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def keypair():
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    priv = key.as_dict(is_private=True)
    priv["kid"] = "kid-recherche"
    pub = key.as_dict(is_private=False)
    pub["kid"] = "kid-recherche"
    return {"private": priv, "public": pub}


def _token(keypair, **over):
    now = int(time.time())
    claims = {
        "sub": "user-a", "iss": ISSUER, "aud": ["mes-reunions"], "azp": "mysearch",
        "iat": now, "exp": now + 300,
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode({"alg": "RS256", "kid": "kid-recherche"}, claims,
                      keypair["private"]).decode("ascii")


# ─── Réponse simulée d'internal-ingester ────────────────────────────────────

def _ingester_body(total=1):
    return {
        "total": total,
        "results": [{
            "uaf_id": UAF,
            "title": "COPIL Nexus – septembre",
            "date": "2026-09-19T08:00:00+00:00",
            "score": 0.61,
            "hits": [
                {"snippet": "On part sur une enveloppe… le budget Nexus.",
                 "highlights": [[34, 40]], "field": "transcript",
                 "location": {"start_seconds": 1421, "end_seconds": 1450, "page": None},
                 "speaker": "M. Bertrand"},
                {"snippet": "Enveloppe 2027 fixée", "highlights": [], "field": "key_points",
                 "location": None, "speaker": None},
            ],
            "context": {"duration_seconds": 4320, "participants_count": 3,
                        "participants_basis": "analysis", "source_type": "upload",
                        "date_kind": "meeting"},
        }],
    }


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def web(monkeypatch, keypair):
    monkeypatch.setenv("SEARCH_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("SEARCH_OIDC_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("MESREUNIONS_CORS_ORIGINS", f"{PORTAIL}, http://localhost:3043")
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC)
    monkeypatch.delenv("SEARCH_OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("SEARCH_ALLOWED_AZP", raising=False)
    mod = _load_web()
    mod.app.config["TESTING"] = True

    from libs.shared.app import oidc_auth
    from app.modules.search import routes
    keys = JsonWebKey.import_key_set({"keys": [keypair["public"]]})
    monkeypatch.setattr(oidc_auth, "fetch_jwks", lambda url, timeout=5: keys)
    oidc_auth.reset_cache_for_tests()
    routes._limiter.reset()

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return _Resp(200, _ingester_body())

    monkeypatch.setattr(routes.req, "post", fake_post)
    uploads = [{"simple_code": "SRCH01", "filename": "SRCH01_copil.mp4"}]
    monkeypatch.setattr(routes, "_live_uploads", lambda sub: uploads)
    return types.SimpleNamespace(client=mod.app.test_client(), routes=routes,
                                 calls=calls, uploads=uploads)


def _get(web, token=None, qs="q=budget", origin=PORTAIL):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if origin:
        headers["Origin"] = origin
    return web.client.get(f"/api/v1/search?{qs}", headers=headers)


def _assert_error(resp, status, code):
    assert resp.status_code == status
    body = resp.get_json()
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert resp.headers["Cache-Control"] == "no-store"


# ─── Jeton ──────────────────────────────────────────────────────────────────

def test_sans_jeton_401(web):
    _assert_error(_get(web), 401, "invalid_token")
    assert web.calls == []


def test_jeton_expire_401(web, keypair):
    old = int(time.time()) - 3600
    _assert_error(_get(web, _token(keypair, iat=old, exp=old + 60)), 401, "invalid_token")


def test_jeton_mal_signe_401(web, keypair):
    other = JsonWebKey.generate_key("RSA", 2048, is_private=True).as_dict(is_private=True)
    other["kid"] = "kid-recherche"
    forged = jwt.encode({"alg": "RS256", "kid": "kid-recherche"},
                        {"sub": "user-a", "iss": ISSUER, "aud": ["mes-reunions"],
                         "azp": "mysearch", "exp": int(time.time()) + 300}, other).decode("ascii")
    _assert_error(_get(web, forged), 401, "invalid_token")


def test_mauvais_emetteur_401(web, keypair):
    _assert_error(_get(web, _token(keypair, iss="https://sso.example.test/realms/autre")),
                  401, "invalid_token")


def test_audience_absente_403(web, keypair):
    _assert_error(_get(web, _token(keypair, aud=["account"])), 403, "audience_mismatch")
    assert web.calls == []


def test_audience_mysearch_seule_403(web, keypair):
    """Confusion d'audience : un jeton frappé pour mysearch n'est pas pour nous."""
    _assert_error(_get(web, _token(keypair, aud=["mysearch"])), 403, "audience_mismatch")


def test_azp_non_autorise_403(web, keypair):
    _assert_error(_get(web, _token(keypair, azp="autre-client")), 403, "audience_mismatch")


def test_jeton_de_mesreunions_elle_meme_403(web, keypair):
    """azp = mes-reunions passe verify_oidc_token mais pas le contrat (aud + azp)."""
    _assert_error(_get(web, _token(keypair, aud=["account"], azp="mes-reunions")),
                  403, "audience_mismatch")


def test_user_sub_vient_du_jeton_pas_des_parametres(web, keypair):
    r = _get(web, _token(keypair, sub="user-a"), qs="q=budget&user_sub=user-b&sub=user-b")
    assert r.status_code == 200
    assert web.calls[0]["json"]["user_sub"] == "user-a"
    assert web.calls[0]["headers"]["Authorization"] == f"Bearer {web.routes.INTERNAL_API_TOKEN}"


def test_cookie_de_session_ne_suffit_pas(web):
    with web.client.session_transaction() as sess:
        sess["user"] = {"sub": "user-a"}
    _assert_error(_get(web), 401, "invalid_token")


# ─── Paramètres ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("qs", [
    "", "q=", "q=%20%20", "q=" + "a" * 1001,
    "q=budget&limit=0", "q=budget&limit=51", "q=budget&limit=dix",
    "q=budget&from=19-09-2026", "q=budget&to=demain",
    "q=budget&from=2026-09-20&to=2026-09-01",
])
def test_parametres_invalides_400(web, keypair, qs):
    _assert_error(_get(web, _token(keypair), qs=qs), 400, "invalid_query")
    assert web.calls == []


def test_parametres_transmis(web, keypair):
    r = _get(web, _token(keypair),
             qs="q=budget%20Nexus&limit=5&scope=inconnu&from=2026-09-01&to=2026-09-30T18:00:00Z")
    assert r.status_code == 200
    sent = web.calls[0]["json"]
    assert sent["q"] == "budget Nexus"
    assert sent["limit"] == 5
    # Date seule : début de journée à l'heure de Paris, bornes incluses.
    assert sent["from"].startswith("2026-09-01T00:00:00")
    assert sent["to"] == "2026-09-30T18:00:00+00:00"
    assert sent["uploads"] == web.uploads
    assert "scope" not in sent


def test_limit_par_defaut_20_et_q_1000(web, keypair):
    r = _get(web, _token(keypair), qs="q=" + "a" * 1000)
    assert r.status_code == 200
    assert web.calls[0]["json"]["limit"] == 20


def test_date_seule_en_fin_couvre_la_journee(web, keypair):
    _get(web, _token(keypair), qs="q=budget&to=2026-09-30")
    assert web.calls[0]["json"]["to"].startswith("2026-09-30T23:59:59")


# ─── Réponse ────────────────────────────────────────────────────────────────

def test_reponse_au_contrat(web, keypair):
    r = _get(web, _token(keypair))
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    body = r.get_json()
    assert body["source"] == "mesreunions"
    assert body["query"] == "budget"
    assert body["total"] == 1 and body["truncated"] is False
    res = body["results"][0]
    assert res["id"] == UAF
    assert res["url"] == f"{PUBLIC}/reunion/{UAF}"
    assert res["context"]["date_kind"] == "meeting"
    t_hit, kp_hit = res["hits"]
    assert t_hit["url"] == f"{PUBLIC}/reunion/{UAF}?t=1421"
    assert t_hit["location"]["start_seconds"] == 1421
    assert t_hit["speaker"] == "M. Bertrand"
    assert kp_hit["location"] is None
    assert kp_hit["url"] == res["url"]


def test_total_exact_par_defaut(web, keypair):
    body = _get(web, _token(keypair)).get_json()
    assert body["total_is_lower_bound"] is False


def test_total_plafonne_est_une_borne_basse(web, keypair, monkeypatch):
    capped = _ingester_body(total=1000)
    capped["total_is_lower_bound"] = True
    monkeypatch.setattr(web.routes.req, "post", lambda *a, **k: _Resp(200, capped))
    body = _get(web, _token(keypair)).get_json()
    assert body["total"] == 1000
    assert body["total_is_lower_bound"] is True
    assert body["truncated"] is True


def test_truncated_si_total_superieur(web, keypair, monkeypatch):
    monkeypatch.setattr(web.routes.req, "post",
                        lambda *a, **k: _Resp(200, _ingester_body(total=7)))
    body = _get(web, _token(keypair)).get_json()
    assert body["total"] == 7 and body["truncated"] is True


def test_url_https_absolue_sans_base_publique_en_production(web, keypair, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = _get(web, _token(keypair)).get_json()
    assert body["results"][0]["url"].startswith("https://")


def test_snippet_texte_brut_transmis_tel_quel(web, keypair, monkeypatch):
    """Un contenu qui ressemble à du HTML reste une donnée : jamais interprété,
    renvoyé en JSON (Mon portail l'affiche en textContent)."""
    body = _ingester_body()
    body["results"][0]["hits"][0]["snippet"] = "<script>alert(1)</script> budget"
    monkeypatch.setattr(web.routes.req, "post", lambda *a, **k: _Resp(200, body))
    r = _get(web, _token(keypair))
    assert r.mimetype == "application/json"
    assert r.get_json()["results"][0]["hits"][0]["snippet"] == "<script>alert(1)</script> budget"


# ─── Indisponibilités ───────────────────────────────────────────────────────

def test_ingester_injoignable_503(web, keypair, monkeypatch):
    def boom(*a, **k):
        raise web.routes.req.ConnectionError("down")
    monkeypatch.setattr(web.routes.req, "post", boom)
    _assert_error(_get(web, _token(keypair)), 503, "search_unavailable")


def test_ingester_en_erreur_503(web, keypair, monkeypatch):
    monkeypatch.setattr(web.routes.req, "post",
                        lambda *a, **k: _Resp(503, {"error": "search_unavailable"}))
    _assert_error(_get(web, _token(keypair)), 503, "search_unavailable")


def test_uploads_illisibles_503(web, keypair, monkeypatch):
    def boom(_sub):
        raise RuntimeError("db down")
    monkeypatch.setattr(web.routes, "_live_uploads", boom)
    _assert_error(_get(web, _token(keypair)), 503, "search_unavailable")
    assert web.calls == []


def test_jwks_injoignable_503(web, keypair, monkeypatch):
    from libs.shared.app import oidc_auth

    def boom(url, timeout=5):
        raise OSError("unreachable")
    monkeypatch.setattr(oidc_auth, "fetch_jwks", boom)
    oidc_auth.reset_cache_for_tests()
    _assert_error(_get(web, _token(keypair)), 503, "search_unavailable")


def test_rate_limit_429_par_utilisateur(web, keypair, monkeypatch):
    from libs.shared.app.rate_limit import SlidingWindowLimiter
    monkeypatch.setattr(web.routes, "_limiter", SlidingWindowLimiter(2, 60))
    tok = _token(keypair)
    assert _get(web, tok).status_code == 200
    assert _get(web, tok).status_code == 200
    r = _get(web, tok)
    _assert_error(r, 429, "rate_limited")
    assert r.headers["Retry-After"] == "60"
    assert r.headers["Access-Control-Allow-Origin"] == PORTAIL
    # Un autre utilisateur n'est pas pénalisé.
    assert _get(web, _token(keypair, sub="user-b")).status_code == 200


# ─── CORS ───────────────────────────────────────────────────────────────────

def test_preflight_origine_autorisee(web):
    r = web.client.options("/api/v1/search?q=budget", headers={
        "Origin": PORTAIL,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    })
    assert r.status_code == 204
    assert r.headers["Access-Control-Allow-Origin"] == PORTAIL
    assert r.headers["Access-Control-Allow-Headers"] == "Authorization"
    assert "GET" in r.headers["Access-Control-Allow-Methods"]
    assert "Origin" in r.headers["Vary"]
    assert "Access-Control-Allow-Credentials" not in r.headers
    assert r.headers["Cache-Control"] == "no-store"


def test_preflight_origine_refusee(web):
    r = web.client.options("/api/v1/search", headers={
        "Origin": "https://intrus.example.test",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code == 403
    assert "Access-Control-Allow-Origin" not in r.headers
    assert "Origin" in r.headers["Vary"]


def test_cors_sur_reponse_et_erreur(web, keypair):
    ok = _get(web, _token(keypair))
    assert ok.headers["Access-Control-Allow-Origin"] == PORTAIL
    assert "Access-Control-Allow-Credentials" not in ok.headers
    ko = _get(web)
    assert ko.status_code == 401
    assert ko.headers["Access-Control-Allow-Origin"] == PORTAIL


def test_cors_origine_inconnue_sans_entete(web, keypair):
    r = _get(web, _token(keypair), origin="https://intrus.example.test")
    assert "Access-Control-Allow-Origin" not in r.headers


def test_cors_limite_au_blueprint(web):
    r = web.client.get("/healthz", headers={"Origin": PORTAIL})
    assert "Access-Control-Allow-Origin" not in r.headers


def test_cors_ferme_si_variable_vide(web, keypair, monkeypatch):
    monkeypatch.setenv("MESREUNIONS_CORS_ORIGINS", "")
    r = _get(web, _token(keypair))
    assert "Access-Control-Allow-Origin" not in r.headers


# ─── Lien /reunion/<id> ─────────────────────────────────────────────────────

def _login(client):
    with client.session_transaction() as sess:
        sess["user"] = {"sub": "user-a", "email": "a@test"}


def test_lien_reunion_ouvre_la_fiche_au_moment(web):
    _login(web.client)
    r = web.client.get(f"/reunion/{UAF}?t=1421")
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert "tab=transfers" in loc and f"file={UAF}" in loc and "t=1421" in loc


def test_lien_reunion_ignore_un_t_invalide(web):
    _login(web.client)
    loc = web.client.get(f"/reunion/{UAF}?t=javascript:alert(1)").headers["Location"]
    assert "t=" not in loc.split("file=")[1]


def test_lien_reunion_identifiant_invalide(web):
    _login(web.client)
    loc = web.client.get("/reunion/pas-un-uuid").headers["Location"]
    assert "file=" not in loc


def test_lien_reunion_passe_par_la_connexion(web):
    r = web.client.get(f"/reunion/{UAF}?t=1421")
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert "/login" in loc
    # Quand require_auth transmet la destination, elle doit être ce lien.
    if "next=" in loc:
        assert "reunion" in loc and "1421" in loc
