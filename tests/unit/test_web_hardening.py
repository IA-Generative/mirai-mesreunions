"""Tests du durcissement HTTP partagé (en-têtes + cookies)."""

from flask import Flask

from libs.shared.app.web_hardening import apply_security_headers


def _app():
    app = Flask(__name__)
    app.secret_key = "k" * 40

    @app.route("/")
    def home():
        return "ok"

    return app


def test_permissions_policy_header_present():
    app = _app()
    apply_security_headers(app)
    resp = app.test_client().get("/")
    assert resp.headers.get("Permissions-Policy") == "geolocation=(), microphone=(self), camera=()"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


def test_cookie_flags_hardened_in_prod(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    app = _app()
    apply_security_headers(app)
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_cookie_secure_relaxed_in_dev(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    app = _app()
    apply_security_headers(app)
    assert app.config["SESSION_COOKIE_SECURE"] is False
