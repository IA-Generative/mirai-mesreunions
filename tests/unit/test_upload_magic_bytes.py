"""Tests de la validation d'upload par magic bytes (durcissement Upload).

Un contenu non-audio déguisé sous une extension audio doit être rejeté ;
les conteneurs audio légitimes sont reconnus par leurs octets de tête.
"""

from libs.shared.app.upload_helpers import (
    sniff_audio_magic,
    looks_like_audio,
    is_allowed_audio_filename,
)


def test_detects_common_audio_containers():
    assert sniff_audio_magic(b"ID3\x04\x00\x00\x00\x00\x00\x00stuff") == "mp3"
    assert sniff_audio_magic(b"\xff\xfb\x90\x00rest of frame") == "mp3"
    assert sniff_audio_magic(b"RIFF\x24\x08\x00\x00WAVEfmt ") == "wav"
    assert sniff_audio_magic(b"OggS\x00\x02\x00\x00\x00\x00\x00\x00") == "ogg"
    assert sniff_audio_magic(b"fLaC\x00\x00\x00\x22") == "flac"
    assert sniff_audio_magic(b"\x00\x00\x00\x20ftypM4A \x00\x00") == "mp4"
    assert sniff_audio_magic(b"\x1aE\xdf\xa3\x01\x00\x00\x00") == "ebml"
    assert sniff_audio_magic(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11") == "asf"


def test_rejects_non_audio_payloads():
    # HTML/script déguisé en .mp3
    assert sniff_audio_magic(b"<html><script>alert(1)</script>") is None
    assert looks_like_audio(b"<html><script>alert(1)</script>") is False
    # Exécutable ELF
    assert sniff_audio_magic(b"\x7fELF\x02\x01\x01\x00") is None
    # PDF
    assert sniff_audio_magic(b"%PDF-1.7\n%...") is None
    # Vide / trop court
    assert sniff_audio_magic(b"") is None
    assert sniff_audio_magic(b"ab") is None


def test_extension_and_magic_are_complementary():
    # Bon nom d'extension mais contenu non-audio ⇒ extension OK, magic KO.
    fake = b"GIF89a\x01\x00\x01\x00"  # image GIF
    assert is_allowed_audio_filename("song.mp3") is True
    assert looks_like_audio(fake) is False
