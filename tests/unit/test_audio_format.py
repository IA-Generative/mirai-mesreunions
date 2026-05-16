"""
Tests pour le helper `to_diarization_format` (workaround pyannote samples
mismatch). On vérifie :
  - le no-op kill-switch (target="mp4") retourne l'input tel quel,
  - un target inconnu logue un warning et fait fallback MP4,
  - le ré-encodage FLAC / WAV produit bien un fichier avec les bons magic
    bytes et un sample-count exactement aligné avec ce qu'on demande
    (le tout l'intérêt du fix),
  - une entrée corrompue déclenche le fallback MP4 plutôt que de crash.

Tests intégration ffmpeg = skippés si l'outil n'est pas dispo en CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import wave

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SERVICE_PATH = os.path.join(ROOT, "services", "dmz-to-internal-bridge")
if SERVICE_PATH not in sys.path:
    sys.path.insert(0, SERVICE_PATH)

from app.audio_format import to_diarization_format  # noqa: E402


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


@pytest.fixture(scope="module")
def sample_mp4_bytes() -> bytes:
    """Produit un MP4/AAC 16 kHz mono 10 s avec une onde sinus (440 Hz)."""
    if not _have_ffmpeg():
        pytest.skip("ffmpeg non installé — tests d'intégration skip")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as out:
        path = out.name
    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10:sample_rate=16000",
            "-ac", "1", "-c:a", "aac", "-b:a", "64k", path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


# ─── No-op cases (pas de ffmpeg requis) ────────────────────────────────────

def test_mp4_target_returns_input_unchanged():
    src = b"any bytes here whatever the actual content"
    body, name, ctype = to_diarization_format(src, "in.mp4", "mp4")
    assert body is src or body == src
    assert name == "in.mp4"
    assert ctype == "audio/mp4"


def test_empty_target_falls_back_to_mp4():
    src = b"data"
    body, name, ctype = to_diarization_format(src, "x.mp4", "")
    assert body == src
    assert ctype == "audio/mp4"


def test_unknown_target_falls_back_with_warning(caplog):
    src = b"data"
    with caplog.at_level("WARNING", logger="app.audio_format"):
        body, name, ctype = to_diarization_format(src, "x.mp4", "ogg")
    assert body == src
    assert ctype == "audio/mp4"
    assert any("Unknown KEVENT_DIARIZATION_FORMAT" in r.message for r in caplog.records)


def test_corrupt_input_triggers_mp4_fallback():
    """Si ffmpeg refuse de décoder, on retombe sur les bytes originaux —
    la diarization tentera quand même, et on évite de casser le pipeline.
    """
    if not _have_ffmpeg():
        pytest.skip("ffmpeg non installé")
    src = b"\x00\x01\x02 not an audio file at all"
    body, name, ctype = to_diarization_format(src, "broken.mp4", "flac")
    assert body == src
    assert ctype == "audio/mp4"


# ─── Re-encode FLAC ────────────────────────────────────────────────────────

def test_reencode_to_flac_produces_flac_magic(sample_mp4_bytes):
    body, name, ctype = to_diarization_format(sample_mp4_bytes, "in.mp4", "flac")
    assert ctype == "audio/flac"
    assert name.endswith(".flac")
    # Magic bytes FLAC = "fLaC" (0x664C6143)
    assert body[:4] == b"fLaC", f"got magic {body[:8]!r}"
    # On ne devrait JAMAIS retomber sur les bytes MP4 d'origine ici
    assert body != sample_mp4_bytes


# ─── Re-encode WAV ─────────────────────────────────────────────────────────

def test_reencode_to_wav_produces_riff_header(sample_mp4_bytes):
    body, name, ctype = to_diarization_format(sample_mp4_bytes, "in.mp4", "wav")
    assert ctype == "audio/wav"
    assert name.endswith(".wav")
    # Magic bytes RIFF/WAVE
    assert body[:4] == b"RIFF", f"got magic {body[:8]!r}"
    assert body[8:12] == b"WAVE"


def test_wav_sample_count_matches_duration(sample_mp4_bytes, tmp_path):
    """Le coeur du workaround : un WAV PCM doit avoir un nombre de
    samples EXACTEMENT cohérent avec sa durée annoncée — c'est ce qui évite
    le "158592 vs 160000" rapporté par pyannote sur le MP4.
    """
    body, _, _ = to_diarization_format(sample_mp4_bytes, "in.mp4", "wav")
    wav_path = tmp_path / "out.wav"
    wav_path.write_bytes(body)
    with wave.open(str(wav_path), "rb") as wf:
        nframes = wf.getnframes()
        sr = wf.getframerate()
        ch = wf.getnchannels()
    assert sr == 16000
    assert ch == 1
    # Source = sine de 10 s à 16 kHz → 160000 samples (avec une marge de
    # ±1 frame pour absorber le delay AAC du fichier source — qui est
    # précisément ce qu'on est en train de NEUTRALISER côté pyannote).
    # Le WAV produit, lui, est déterministe à round-trip.
    assert 159000 <= nframes <= 161000, f"got {nframes} frames"


def test_filename_stem_preserved(sample_mp4_bytes):
    """Le stem du nom de fichier source doit être conservé (pour la
    traçabilité dans les logs Kevent + métriques)."""
    _, name, _ = to_diarization_format(
        sample_mp4_bytes, "9NXBMN_abc123_Echange_IA.mp4", "flac",
    )
    assert name == "9NXBMN_abc123_Echange_IA.flac"


def test_path_in_filename_is_stripped(sample_mp4_bytes):
    """Si on passe un path complet, on garde juste le basename."""
    _, name, _ = to_diarization_format(
        sample_mp4_bytes, "/tmp/some/dir/foo.mp4", "wav",
    )
    assert name == "foo.wav"
