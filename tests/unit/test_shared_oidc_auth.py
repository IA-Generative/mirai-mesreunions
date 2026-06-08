"""Tests de la brique d'authentification OIDC partagée (``libs/shared/app/oidc_auth.py``).

Cette brique centralise la vérification JWT/OIDC pour tous les services
(généralisation de la brique JWKS éprouvée du connecteur vidéo) afin que la
vérification soit homogène et fail-closed d'un service à l'autre.

Couverture :
  - signature : token signé valide accepté ; signature falsifiée rejetée ;
    ``alg:none`` rejeté.
  - audience OBLIGATOIRE : appel sans audience attendue refusé (fail-closed) ;
    token frappé pour un autre client du realm rejeté (confusion d'audience).
  - id_token : nonce absent/incorrect rejeté ; nonce correct accepté.
  - garde de démarrage : bypass d'auth refusé en prod, conservé en dev/test ;
    AUTH_MODE=gateway exige un secret de confiance (fail-closed sinon).
  - autorisation admin : liste vide ⇒ refus (fail-closed).
"""

import time

import pytest
from authlib.jose import JsonWebKey, jwt

from libs.shared.app import oidc_auth


# ─── Helpers : génération d'une paire de clés + tokens signés ──────────────

@pytest.fixture(scope="module")
def keypair():
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    kid = "test-kid-1"
    priv = key.as_dict(is_private=True)
    priv["kid"] = kid
    pub = key.as_dict(is_private=False)
    pub["kid"] = kid
    return {"private": priv, "public": pub, "kid": kid}


@pytest.fixture
def jwks_keys(keypair):
    return JsonWebKey.import_key_set({"keys": [keypair["public"]]})


@pytest.fixture(autouse=True)
def _patch_jwks(monkeypatch, jwks_keys):
    """Évite tout appel réseau : la brique charge le JWKS via ``fetch_jwks``."""
    monkeypatch.setattr(oidc_auth, "fetch_jwks", lambda url, timeout=5: jwks_keys)
    oidc_auth.reset_cache_for_tests()


def _sign(keypair, claims, alg="RS256"):
    header = {"alg": alg, "kid": keypair["kid"]}
    return jwt.encode(header, claims, keypair["private"]).decode("ascii")


ISSUER = "https://sso.example.test/realms/openwebui"
AUD = "mes-reunions"
JWKS_URL = "https://sso.example.test/realms/openwebui/protocol/openid-connect/certs"


def _base_claims(**over):
    now = int(time.time())
    c = {
        "sub": "user-123",
        "iss": ISSUER,
        "aud": AUD,
        "iat": now,
        "exp": now + 3600,
    }
    c.update(over)
    return c


# ─── verify_oidc_token : signature + audience obligatoire ─────────────────

def test_valid_token_accepted(keypair):
    token = _sign(keypair, _base_claims())
    claims = oidc_auth.verify_oidc_token(
        token, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL
    )
    assert claims["sub"] == "user-123"


def test_tampered_signature_rejected(keypair):
    token = _sign(keypair, _base_claims())
    # Corrompt la signature (dernier segment).
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}.{sig[:-3]}AAA"
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_oidc_token(
            tampered, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL
        )


def test_alg_none_rejected(keypair):
    # Token "alg: none" forgé manuellement.
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(_base_claims())}."
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_oidc_token(
            forged, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL
        )


def test_audience_is_mandatory(keypair):
    """Sans audience attendue, la vérification refuse (fail-closed)."""
    token = _sign(keypair, _base_claims())
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_oidc_token(
            token, audience=None, issuer=ISSUER, jwks_url=JWKS_URL
        )
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_oidc_token(
            token, audience="", issuer=ISSUER, jwks_url=JWKS_URL
        )


def test_audience_confusion_rejected(keypair):
    """Un token frappé pour un AUTRE client du realm est rejeté."""
    token = _sign(keypair, _base_claims(aud="autre-client"))
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_oidc_token(
            token, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL
        )


def test_audience_accepted_via_azp(keypair):
    """Token réel Keycloak : `aud` cible les resource servers aval, le client
    appelant est dans `azp`. L'audience attendue == azp ⇒ accepté."""
    token = _sign(keypair, _base_claims(aud=["drive", "mcr", "account"], azp=AUD))
    claims = oidc_auth.verify_oidc_token(
        token, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL
    )
    assert claims["sub"] == "user-123"


def test_audience_rejected_when_neither_aud_nor_azp(keypair):
    """Token d'un autre client : azp différent et pas dans aud ⇒ rejeté."""
    token = _sign(keypair, _base_claims(aud=["drive", "account"], azp="autre-client"))
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_oidc_token(
            token, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL
        )


def test_expired_token_rejected(keypair):
    token = _sign(keypair, _base_claims(exp=int(time.time()) - 10))
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_oidc_token(
            token, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL
        )


def test_wrong_issuer_rejected(keypair):
    token = _sign(keypair, _base_claims(iss="https://evil.example/realms/x"))
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_oidc_token(
            token, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL
        )


# ─── verify_id_token : signature + nonce ──────────────────────────────────

def test_id_token_valid_nonce_accepted(keypair):
    token = _sign(keypair, _base_claims(nonce="abc123"))
    claims = oidc_auth.verify_id_token(
        token, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL, nonce="abc123"
    )
    assert claims["sub"] == "user-123"


def test_id_token_missing_nonce_rejected(keypair):
    token = _sign(keypair, _base_claims())  # pas de claim nonce
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_id_token(
            token, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL, nonce="abc123"
        )


def test_id_token_wrong_nonce_rejected(keypair):
    token = _sign(keypair, _base_claims(nonce="server-side"))
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_id_token(
            token, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL, nonce="attacker"
        )


def test_id_token_requires_expected_nonce(keypair):
    """Si le serveur n'a pas de nonce en session, on refuse (anti-rejeu)."""
    token = _sign(keypair, _base_claims(nonce="x"))
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_id_token(
            token, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL, nonce=""
        )


def test_id_token_tampered_signature_rejected(keypair):
    token = _sign(keypair, _base_claims(nonce="n"))
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}.{sig[:-3]}AAA"
    with pytest.raises(oidc_auth.OidcAuthError):
        oidc_auth.verify_id_token(
            tampered, audience=AUD, issuer=ISSUER, jwks_url=JWKS_URL, nonce="n"
        )


# ─── assert_auth_startup_config : garde de démarrage fail-closed ───────────

def test_auth_bypass_refused_in_prod():
    env = {"ENVIRONMENT": "production", "VIDEO_INGEST_AUTH_DISABLED": "1"}
    with pytest.raises(oidc_auth.AuthStartupError):
        oidc_auth.assert_auth_startup_config(
            env, service_name="video-ingest",
            auth_disable_flags=("VIDEO_INGEST_AUTH_DISABLED", "VITE_DEV"),
        )


def test_auth_bypass_allowed_in_dev():
    env = {"ENVIRONMENT": "development", "VIDEO_INGEST_AUTH_DISABLED": "1"}
    # Ne lève pas en dev.
    oidc_auth.assert_auth_startup_config(
        env, service_name="video-ingest",
        auth_disable_flags=("VIDEO_INGEST_AUTH_DISABLED",),
    )


def test_auth_bypass_refused_in_prod_beta():
    env = {"ENVIRONMENT": "prod-beta", "VITE_DEV": "1"}
    with pytest.raises(oidc_auth.AuthStartupError):
        oidc_auth.assert_auth_startup_config(
            env, service_name="video-ingest",
            auth_disable_flags=("VITE_DEV",),
        )


def test_gateway_mode_requires_trust_secret():
    env = {"ENVIRONMENT": "production", "AUTH_MODE": "gateway"}
    with pytest.raises(oidc_auth.AuthStartupError):
        oidc_auth.assert_auth_startup_config(env, service_name="web")


def test_gateway_mode_ok_with_strong_secret():
    env = {
        "ENVIRONMENT": "production",
        "AUTH_MODE": "gateway",
        "AUTH_GATEWAY_SHARED_SECRET": "x" * 40,
    }
    oidc_auth.assert_auth_startup_config(env, service_name="web")


def test_unknown_auth_mode_refused():
    env = {"ENVIRONMENT": "production", "AUTH_MODE": "wide-open"}
    with pytest.raises(oidc_auth.AuthStartupError):
        oidc_auth.assert_auth_startup_config(env, service_name="web")


def test_clean_prod_config_passes():
    env = {"ENVIRONMENT": "production"}
    oidc_auth.assert_auth_startup_config(
        env, service_name="web",
        auth_disable_flags=("VIDEO_INGEST_AUTH_DISABLED",),
    )


# ─── is_user_admin : autorisation admin fail-closed ───────────────────────

def test_admin_empty_allowlist_denies():
    """Liste d'accès vide ⇒ refus (fail-closed), jamais fail-open."""
    user = {"preferred_username": "alice", "email": "alice@example.test", "sub": "s"}
    assert oidc_auth.is_user_admin(user, set()) is False
    assert oidc_auth.is_user_admin(user, None) is False


def test_admin_allowlisted_user_allowed():
    user = {"preferred_username": "alice", "email": "alice@example.test", "sub": "s"}
    assert oidc_auth.is_user_admin(user, {"alice"}) is True
    assert oidc_auth.is_user_admin(user, {"alice@example.test"}) is True


def test_admin_non_allowlisted_denied():
    user = {"preferred_username": "bob", "email": "bob@example.test", "sub": "s"}
    assert oidc_auth.is_user_admin(user, {"alice"}) is False


def test_assert_admin_allowlist_configured_refuses_empty():
    with pytest.raises(oidc_auth.AuthStartupError):
        oidc_auth.assert_admin_allowlist_configured(set())
    # Une liste non vide passe.
    oidc_auth.assert_admin_allowlist_configured({"admin"})


# ─── Admin par groupe Keycloak (/g/admins) ─────────────────────

def test_admin_via_group_membership():
    user = {"sub": "s", "groups": ["/g/users", "/g/admins"]}
    assert oidc_auth.is_user_admin(user) is True


def test_non_member_of_admin_group_denied():
    user = {"sub": "s", "groups": ["/g/users"]}
    # Pas dans le groupe admin et pas d'allowlist ⇒ refus.
    assert oidc_auth.is_user_admin(user) is False
    assert oidc_auth.is_user_admin(user, set()) is False


def test_admin_group_match_is_case_and_slash_tolerant():
    user = {"sub": "s", "groups": ["g/Admins"]}  # sans slash, casse
    assert oidc_auth.is_user_admin(user) is True


def test_admin_group_overridable_by_env(monkeypatch):
    monkeypatch.setenv("ADMIN_GROUP", "/g/autre-groupe")
    member = {"sub": "s", "groups": ["/g/autre-groupe"]}
    notmember = {"sub": "s", "groups": ["/g/admins"]}
    assert oidc_auth.is_user_admin(member) is True
    assert oidc_auth.is_user_admin(notmember) is False


def test_admin_group_explicit_param():
    user = {"sub": "s", "groups": ["/g/x"]}
    assert oidc_auth.is_user_admin(user, admin_group="/g/x") is True
    assert oidc_auth.is_user_admin(user, admin_group="/g/y") is False


def test_allowlist_still_works_as_breakglass():
    user = {"preferred_username": "alice", "groups": []}
    assert oidc_auth.is_user_admin(user, {"alice"}) is True


def test_assert_admin_access_configured_accepts_group():
    # Liste vide mais groupe configuré (défaut) ⇒ OK.
    oidc_auth.assert_admin_access_configured(set())
    # Ni groupe ni allowlist ⇒ refus.
    with pytest.raises(oidc_auth.AuthStartupError):
        oidc_auth.assert_admin_access_configured(set(), admin_group="")
