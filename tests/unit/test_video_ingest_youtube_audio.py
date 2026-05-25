"""Tests du fallback ASR — mocks yt-dlp + Kevent.

Critique : on vérifie aussi qu'aucun fichier audio ne survit après
l'appel (DoD §10, Principe 2).
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.video_ingest.app.providers.base import ProviderError, VideoUnavailable
from services.video_ingest.app.providers.youtube import audio as audio_mod
from services.video_ingest.app.providers.youtube._kevent import KeventError


def _ydl_writes_one_audio(target_dir_ref):
    """Construit un mock YoutubeDL qui écrit 1 fichier .m4a fake dans tmp."""
    def _ydl_ctx(opts):
        target_dir_ref["dir"] = Path(opts["outtmpl"]).parent

        class _YDL:
            def __enter__(self_):
                return self_
            def __exit__(self_, *a):
                return False
            def download(self_, urls):
                p = Path(opts["outtmpl"].replace("%(id)s", "fake").replace("%(ext)s", "m4a"))
                p.write_bytes(b"fake-audio-bytes")
        return _YDL()
    return _ydl_ctx


def _kevent_returns(transcript_payload):
    submit = MagicMock(return_value="job-42")
    wait = MagicMock(return_value=transcript_payload)
    return submit, wait


def test_fetch_audio_full_pipeline_and_cleans_up():
    target_dir_ref = {}
    payload = {
        "language": "fr",
        "segments": [
            {"text": "bonjour", "start": 0.0, "end": 2.5},
            {"text": "monde", "start": 2.5, "end": 4.0},
        ],
    }
    submit, wait = _kevent_returns(payload)

    with patch.object(audio_mod, "YoutubeDL", _ydl_writes_one_audio(target_dir_ref)), \
         patch.object(audio_mod._kevent, "submit_transcription", submit), \
         patch.object(audio_mod._kevent, "wait_for_result", wait):
        result = audio_mod.fetch_audio_and_transcribe("dQw4w9WgXcQ", language="fr")

    # Pipeline complet appelé
    assert submit.called
    assert wait.called_with("job-42")
    # Transcript correctement mappé
    assert result.language == "fr"
    assert result.method == "asr_whisper_kevent"
    assert len(result.segments) == 2
    assert result.segments[0].text == "bonjour"
    assert result.segments[0].duration_seconds == 2.5
    # DoD §10 : le TemporaryDirectory a été nettoyé.
    assert not target_dir_ref["dir"].exists(), (
        f"Le répertoire temp {target_dir_ref['dir']} aurait dû être supprimé"
    )


def test_fetch_audio_empty_text_payload_fallback_to_single_segment():
    target_dir_ref = {}
    payload = {"language": "fr", "text": "tout le texte sans segments"}
    submit, wait = _kevent_returns(payload)

    with patch.object(audio_mod, "YoutubeDL", _ydl_writes_one_audio(target_dir_ref)), \
         patch.object(audio_mod._kevent, "submit_transcription", submit), \
         patch.object(audio_mod._kevent, "wait_for_result", wait):
        result = audio_mod.fetch_audio_and_transcribe("dQw4w9WgXcQ")

    assert len(result.segments) == 1
    assert result.segments[0].text == "tout le texte sans segments"


def test_fetch_audio_empty_payload_raises():
    target_dir_ref = {}
    payload = {}  # ni segments ni text → erreur
    submit, wait = _kevent_returns(payload)

    with patch.object(audio_mod, "YoutubeDL", _ydl_writes_one_audio(target_dir_ref)), \
         patch.object(audio_mod._kevent, "submit_transcription", submit), \
         patch.object(audio_mod._kevent, "wait_for_result", wait):
        with pytest.raises(ProviderError):
            audio_mod.fetch_audio_and_transcribe("dQw4w9WgXcQ")
    # DoD §10 même en cas d'erreur
    assert not target_dir_ref["dir"].exists()


def test_fetch_audio_ytdlp_unavailable_maps_correctly():
    from yt_dlp.utils import DownloadError

    def raising_ydl(opts):
        class _YDL:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def download(self_, urls):
                raise DownloadError("Video is private")
        return _YDL()

    with patch.object(audio_mod, "YoutubeDL", raising_ydl):
        with pytest.raises(VideoUnavailable):
            audio_mod.fetch_audio_and_transcribe("dQw4w9WgXcQ")


def test_kevent_submit_requires_env(monkeypatch):
    monkeypatch.delenv("VIDEO_INGEST_KEVENT_GATEWAY_URL", raising=False)
    monkeypatch.delenv("VIDEO_INGEST_KEVENT_API_KEY", raising=False)
    from services.video_ingest.app.providers.youtube import _kevent
    with pytest.raises(KeventError):
        _kevent.submit_transcription(b"x", "x.m4a")


def test_kevent_submit_propagates_http_error(monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_KEVENT_GATEWAY_URL", "http://example/")
    monkeypatch.setenv("VIDEO_INGEST_KEVENT_API_KEY", "k")
    from services.video_ingest.app.providers.youtube import _kevent
    fake_resp = MagicMock(status_code=502, text="bad gateway")
    with patch("services.video_ingest.app.providers.youtube._kevent.requests.post",
               return_value=fake_resp):
        with pytest.raises(KeventError):
            _kevent.submit_transcription(b"x", "x.m4a")