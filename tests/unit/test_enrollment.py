"""Cœur partagé d'émission du QR (mesreunions-web + qr-enrollment-portal)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from libs.shared.app import enrollment
from libs.shared.app.enrollment import enrich_devices, make_qr_png, upload_url_for

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def test_qr_png_and_upload_url():
    assert make_qr_png("https://depot.example.fr/upload/abc").startswith(b"\x89PNG")
    assert upload_url_for("https://depot.example.fr/", "tok") == "https://depot.example.fr/upload/tok"


def test_request_token_from_internal(monkeypatch):
    captured = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"qr_token": "t", "simple_code": "C", "expires_at": NOW.isoformat()}

    def post(url, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers)
        return Resp()

    monkeypatch.setattr(enrollment.req, "post", post)
    user = {"sub": "s", "email": "e@x.fr", "name": "N"}
    assert enrollment.request_token_from_internal(user, 15, 5, auto_transcribe=False)["qr_token"] == "t"
    assert captured["json"]["user_sub"] == "s"
    assert captured["json"]["auto_transcribe"] is False
    assert "ttl_seconds" not in captured["json"]
    assert captured["headers"]["Authorization"].startswith("Bearer ")


def _session(token, created, uploads=(), max_uploads=10, upload_count=0, expires_in=30):
    return SimpleNamespace(
        qr_token=token, created_at=created, max_uploads=max_uploads,
        upload_count=upload_count, simple_code=f"C-{token}",
        expires_at=(NOW + timedelta(days=expires_in)).replace(tzinfo=None),
        uploads=[SimpleNamespace(created_at=c) for c in uploads],
    )


def test_enrich_devices_uses_latest_session():
    old = _session("t1", NOW - timedelta(days=2), max_uploads=1)
    new = _session("t1", NOW - timedelta(hours=1), uploads=[NOW - timedelta(hours=2),
                                                         NOW - timedelta(days=3), None],
                   upload_count=3, expires_in=3)
    (device,) = enrich_devices([{"device_id": "d", "qr_token": "t1"}], [old, new], now=NOW)
    assert device["session_max_uploads"] == 10
    assert device["remaining_uploads"] == 7
    assert device["recent_uploads_24h"] == 1
    assert device["session_expiring_soon"] is True


def test_enrich_devices_without_session():
    (device,) = enrich_devices([{"device_id": "d", "qr_token": "unknown"}], [], now=NOW)
    assert device["session_simple_code"] is None
    assert device["remaining_uploads"] == 0
