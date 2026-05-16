"""
Unit tests for the auto-loudnorm decision (RMS probe).

The transcode worker probes a few small windows of the input and decides
whether to run the expensive 2-pass loudnorm. These tests mock ffmpeg so
they don't need a real binary, and verify the decision logic + edge cases.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# Stub the libs.shared.app.config module the worker imports — only the names it uses.
# This avoids pulling in the whole libs.shared.app package (which would require
# sqlalchemy, boto3, pika etc. installed for a unit test that doesn't need them).
def _install_stubs():
    libs = types.ModuleType("libs")
    libs_shared = types.ModuleType("libs.shared")
    libs_shared_app = types.ModuleType("libs.shared.app")
    config_stub = types.ModuleType("libs.shared.app.config")
    config_stub.load_ext_db = MagicMock(return_value=MagicMock())
    config_stub.load_s3_upload = MagicMock(return_value=MagicMock())
    config_stub.load_s3_processed = MagicMock(return_value=MagicMock())
    config_stub.RabbitMQConfig = MagicMock()
    config_stub.TRANSCODE_SAMPLE_RATE = 16000
    config_stub.TRANSCODE_CHANNELS = 1
    config_stub.INTERNAL_API_TOKEN = "x" * 24
    config_stub.LOUDNORM_AUTO_DECISION = True
    config_stub.LOUDNORM_RMS_THRESHOLD_DBFS = -30.0
    config_stub.LOUDNORM_PROBE_OFFSETS_S = "60,300"
    config_stub.LOUDNORM_PROBE_DURATION_S = 5.0

    models_stub = types.ModuleType("libs.shared.app.models")
    for n in ("ExternalBase", "UploadedFile", "UploadSession", "UploadStatus"):
        setattr(models_stub, n, MagicMock())

    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = MagicMock()
    db_stub.init_tables = MagicMock()

    s3_stub = types.ModuleType("libs.shared.app.s3_helper")
    s3_stub.download_fileobj = MagicMock()
    s3_stub.upload_fileobj = MagicMock()
    s3_stub.ensure_bucket = MagicMock()

    queue_stub = types.ModuleType("libs.shared.app.queue_helper")
    queue_stub.consume_queue = MagicMock()
    queue_stub.publish_message = MagicMock()
    queue_stub.declare_queues = MagicMock()
    queue_stub.QUEUE_TRANSCODE = "transcode"
    queue_stub.QUEUE_FILE_READY = "file_ready"
    queue_stub.RabbitMQConfig = MagicMock()

    sec_stub = types.ModuleType("libs.shared.app.security")
    sec_stub.require_strong_shared_secret = MagicMock()

    for name, m in [
        ("libs", libs), ("libs.shared", libs_shared), ("libs.shared.app", libs_shared_app),
        ("libs.shared.app.config", config_stub),
        ("libs.shared.app.models", models_stub),
        ("libs.shared.app.database", db_stub),
        ("libs.shared.app.s3_helper", s3_stub),
        ("libs.shared.app.queue_helper", queue_stub),
        ("libs.shared.app.security", sec_stub),
    ]:
        sys.modules[name] = m


_install_stubs()


def _import_worker():
    path = os.path.join(ROOT, "services", "audio-normalizer", "app", "main.py")
    spec = importlib.util.spec_from_file_location("audio_normalizer_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WORKER = _import_worker()


# ─── measure_rms_dbfs ──────────────────────────────────────────────────────

def test_measure_rms_returns_value_when_ffmpeg_emits_metric():
    fake = MagicMock()
    fake.returncode = 0
    fake.stderr = "[Parsed_astats_0 @ 0x...] Channel: 1\nRMS level dB: -18.45\nPeak level dB: -3.02\n"
    with patch("subprocess.run", return_value=fake):
        v = WORKER.measure_rms_dbfs("/tmp/x.wav", 60.0, 5.0)
    assert v == pytest.approx(-18.45)


def test_measure_rms_returns_none_on_ffmpeg_error():
    fake = MagicMock()
    fake.returncode = 1
    fake.stderr = "ffmpeg: error opening file"
    with patch("subprocess.run", return_value=fake):
        assert WORKER.measure_rms_dbfs("/tmp/x.wav", 60, 5) is None


def test_measure_rms_returns_none_when_no_match_in_stderr():
    fake = MagicMock()
    fake.returncode = 0
    fake.stderr = "no metric here"
    with patch("subprocess.run", return_value=fake):
        assert WORKER.measure_rms_dbfs("/tmp/x.wav", 60, 5) is None


def test_measure_rms_handles_timeout_gracefully():
    import subprocess
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
        assert WORKER.measure_rms_dbfs("/tmp/x.wav", 60, 5) is None


# ─── needs_loudnorm ───────────────────────────────────────────────────────

def test_needs_loudnorm_skips_when_audio_is_loud_enough(monkeypatch):
    """Audio louder than threshold (-30 dBFS by default) → skip loudnorm."""
    monkeypatch.setattr(WORKER, "measure_rms_dbfs", lambda *a, **kw: -18.0)
    needed, info = WORKER.needs_loudnorm("/tmp/x.wav", file_duration_s=600.0)
    assert needed is False
    assert info["decision"] == "skip"
    assert info["max_rms_dbfs"] == -18.0


def test_needs_loudnorm_normalizes_when_audio_is_faint(monkeypatch):
    """Audio quieter than threshold → normalize."""
    monkeypatch.setattr(WORKER, "measure_rms_dbfs", lambda *a, **kw: -42.0)
    needed, info = WORKER.needs_loudnorm("/tmp/x.wav", file_duration_s=600.0)
    assert needed is True
    assert info["decision"] == "loudnorm"


def test_needs_loudnorm_uses_loudest_window(monkeypatch):
    """Decision uses the MAX RMS across windows — one loud window suffices to skip."""
    samples = iter([-50.0, -10.0])  # first quiet, second loud
    monkeypatch.setattr(WORKER, "measure_rms_dbfs", lambda *a, **kw: next(samples))
    needed, info = WORKER.needs_loudnorm("/tmp/x.wav", file_duration_s=600.0)
    assert needed is False  # loudest sample (-10) > threshold
    assert info["max_rms_dbfs"] == -10.0


def test_needs_loudnorm_falls_back_to_start_for_short_files(monkeypatch):
    """Files shorter than the smallest probe offset get a single sample at 0."""
    calls = []
    def fake(input_path, offset, duration):
        calls.append((offset, duration))
        return -25.0
    monkeypatch.setattr(WORKER, "measure_rms_dbfs", fake)
    needed, info = WORKER.needs_loudnorm("/tmp/x.wav", file_duration_s=10.0)
    assert calls == [(0.0, WORKER.LOUDNORM_PROBE_DURATION_S)]


def test_needs_loudnorm_defaults_to_normalize_when_probe_fails(monkeypatch):
    """If every ffmpeg probe failed, be conservative and apply loudnorm."""
    monkeypatch.setattr(WORKER, "measure_rms_dbfs", lambda *a, **kw: None)
    needed, info = WORKER.needs_loudnorm("/tmp/x.wav", file_duration_s=600.0)
    assert needed is True
    assert "probe failed" in info["reason"]


def test_needs_loudnorm_handles_malformed_offsets_env(monkeypatch):
    """Malformed CSV in LOUDNORM_PROBE_OFFSETS_S falls back to defaults."""
    monkeypatch.setattr(WORKER, "LOUDNORM_PROBE_OFFSETS_S", "not_a_number")
    monkeypatch.setattr(WORKER, "measure_rms_dbfs", lambda *a, **kw: -25.0)
    needed, info = WORKER.needs_loudnorm("/tmp/x.wav", file_duration_s=600.0)
    # Falls back to [60, 300] then keeps both since duration is 600s
    assert "max_rms_dbfs" in info  # didn't crash
