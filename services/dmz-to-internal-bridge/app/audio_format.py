"""
Helper de ré-encodage audio pour la soumission Kevent.

Workaround pour le bug pyannote-audio "samples mismatch" rencontré sur les
fichiers MP4/AAC : l'encoder delay AAC (~2112 samples de priming) + le
frame padding font diverger la durée décodée de la durée annoncée dans le
header MP4. Pyannote chunke en se fiant au header (10 s × 16 kHz = 160 000
samples) mais le décodeur en sort ~158 600 → HTTP 500.

Re-encoder en FLAC (lossless) ou en PCM WAV avant la soumission supprime
le drift par construction : ces deux conteneurs encodent les samples
bit-pour-bit avec la durée du header.

Module séparé (vs. inline dans puller.py) pour pouvoir le tester sans
charger la stack dmz-to-internal-bridge entière (kevent_client + DB + Flask).
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from typing import Tuple

logger = logging.getLogger(__name__)


# Tableau de configuration ffmpeg par format cible. `codec_args` est inséré
# après les `-ar` / `-ac` génériques. `ext` et `content_type` pilotent ce
# qu'on annonce au gateway Kevent dans le multipart.
_FFMPEG_SPECS = {
    "flac": {
        "ext": ".flac",
        "content_type": "audio/flac",
        # compression_level=5 = défaut FLAC, ratio/CPU équilibré (~30 MB/h
        # pour 16 kHz mono). Bit-exact à round-trip par spec.
        "codec_args": ["-c:a", "flac", "-compression_level", "5"],
    },
    "wav": {
        "ext": ".wav",
        "content_type": "audio/wav",
        # PCM signed 16-bit little-endian dans un container RIFF/WAV.
        # ~115 MB/h pour 16 kHz mono, mais 0 surcoût CPU au décodage côté
        # pyannote.
        "codec_args": ["-c:a", "pcm_s16le", "-f", "wav"],
    },
}


def to_diarization_format(
    audio_bytes: bytes,
    src_filename: str,
    target: str,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    timeout_s: int = 180,
) -> Tuple[bytes, str, str]:
    """Ré-encode l'audio en mémoire au format ``target`` (flac / wav / mp4).

    Args:
        audio_bytes: contenu du fichier source (typiquement MP4/AAC du
            audio-normalizer).
        src_filename: nom de fichier source — utilisé pour dériver le stem
            du nouveau nom et pour les logs.
        target: ``"flac"``, ``"wav"``, ou ``"mp4"`` (no-op kill-switch).
            Tout autre valeur ⇒ no-op + warning log.
        sample_rate, channels: forcés via ``-ar`` / ``-ac``.
        timeout_s: kill ffmpeg si la conversion dépasse ce délai
            (sécurité contre un ffmpeg qui rame sur un fichier corrompu).

    Returns:
        ``(bytes, filename, content_type)``. En cas d'erreur ffmpeg
        (returncode != 0, timeout, ou target inconnu non-mp4), retourne
        les bytes originaux avec ``content_type="audio/mp4"`` — la
        diarization tentera quand même, et au pire on revient au comportement
        d'avant le workaround.
    """
    target = (target or "mp4").lower()
    fallback = (audio_bytes, src_filename, "audio/mp4")

    if target == "mp4":
        return fallback
    if target not in _FFMPEG_SPECS:
        logger.warning(
            "Unknown KEVENT_DIARIZATION_FORMAT=%r — supported: 'flac', 'wav', 'mp4'. "
            "Falling back to MP4 (no re-encode).",
            target,
        )
        return fallback

    spec = _FFMPEG_SPECS[target]
    base = (src_filename or "audio").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    out_name = f"{stem}{spec['ext']}"

    # On passe par des fichiers temp (et pas par pipe stdin/stdout) parce
    # que ffmpeg a besoin de seek pour :
    #   - FLAC : écrire le MD5 dans streaminfo après l'encodage.
    #   - WAV  : patcher la taille du data-chunk dans le header à la fin.
    with tempfile.NamedTemporaryFile(suffix=".mp4") as inp, \
            tempfile.NamedTemporaryFile(suffix=spec["ext"]) as out:
        inp.write(audio_bytes)
        inp.flush()
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
            "-i", inp.name,
            "-ar", str(sample_rate),
            "-ac", str(channels),
            *spec["codec_args"],
            out.name,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            logger.warning(
                "diarization re-encode timed out after %ss (target=%s) — falling back to MP4 for %s",
                timeout_s, target, src_filename,
            )
            return fallback
        if result.returncode != 0:
            stderr_tail = (result.stderr or b"")[-400:].decode("utf-8", errors="replace")
            logger.warning(
                "diarization re-encode failed (target=%s, rc=%s) — falling back to MP4. ffmpeg stderr: %s",
                target, result.returncode, stderr_tail,
            )
            return fallback
        out.seek(0)
        body = out.read()

    if not body:
        logger.warning(
            "diarization re-encode produced empty output (target=%s) — falling back to MP4",
            target,
        )
        return fallback

    logger.info(
        "diarization re-encoded: %s → %s (target=%s, %d → %d bytes)",
        src_filename, out_name, target, len(audio_bytes), len(body),
    )
    return body, out_name, spec["content_type"]
