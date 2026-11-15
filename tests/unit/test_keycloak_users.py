"""Résolution e-mail → compte Keycloak via l'API d'administration."""

import pytest

from libs.shared.app.keycloak_users import (
    AmbiguousUserError,
    KeycloakLookupError,
    KeycloakUserResolver,
)


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, users):
        self.users = users
        self.token_calls = 0
        self.lookups = []

    def post(self, url, data, timeout):
        assert url.endswith("/realms/openwebui/protocol/openid-connect/token")
        assert data["grant_type"] == "client_credentials"
        self.token_calls += 1
        return FakeResponse({"access_token": f"t{self.token_calls}", "expires_in": 300})

    def get(self, url, params, headers, timeout):
        assert url.endswith("/admin/realms/openwebui/users")
        assert params["exact"] == "true"
        self.lookups.append((params["email"], headers["Authorization"]))
        if isinstance(self.users, Exception):
            raise self.users
        return FakeResponse(self.users)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def resolver(users, clock=None):
    http = FakeHttp(users)
    r = KeycloakUserResolver("http://kc:8080/", "openwebui", "qr-portal", "secret",
                             http=http, clock=clock or Clock())
    return r, http


USER = {"id": "sub-123", "email": "jeanne.martin@x.fr", "firstName": "Jeanne",
        "lastName": "Martin", "enabled": True}


def test_resolves_an_active_account():
    r, http = resolver([USER])
    assert r.resolve_by_email("Jeanne.Martin@x.fr") == {
        "sub": "sub-123", "email": "jeanne.martin@x.fr", "name": "Jeanne Martin",
    }
    assert http.lookups == [("jeanne.martin@x.fr", "Bearer t1")]


def test_no_account_and_disabled_account():
    assert resolver([])[0].resolve_by_email("a@x.fr") is None
    assert resolver([{**USER, "enabled": False}])[0].resolve_by_email(USER["email"]) is None


def test_ignores_non_exact_matches():
    assert resolver([{**USER, "email": "other@x.fr"}])[0].resolve_by_email(USER["email"]) is None


def test_ambiguous_accounts():
    with pytest.raises(AmbiguousUserError):
        resolver([USER, {**USER, "id": "sub-456"}])[0].resolve_by_email(USER["email"])


def test_keycloak_errors_are_transient():
    r, _ = resolver(ConnectionError("down"))
    with pytest.raises(KeycloakLookupError):
        r.resolve_by_email(USER["email"])


def test_caches_lookups_and_admin_token():
    clock = Clock()
    r, http = resolver([USER], clock)
    r.resolve_by_email(USER["email"])
    r.resolve_by_email(USER["email"])
    assert len(http.lookups) == 1
    clock.now += 301
    r.resolve_by_email(USER["email"])
    assert len(http.lookups) == 2
    # Jeton réutilisé pendant sa validité (expires_in - 30 s), renouvelé après.
    assert http.token_calls == 2


def test_incomplete_configuration():
    with pytest.raises(ValueError):
        KeycloakUserResolver("http://kc", "openwebui", "", "")
