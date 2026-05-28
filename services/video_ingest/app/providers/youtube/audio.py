"""Téléchargement audio YouTube + transcription Kevent.

CRITIQUE (cf. Principe 2 + DoD §10) : aucun fichier audio n'est
persisté après transcription. On utilise un `TemporaryDirectory` qui
nettoie au sortir du `with`, et un test E2E vérifie qu'il ne reste rien.

Cf. INTEGRATION_NOTES.md §2 pour la stratégie globale (chemin A = Kevent).
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ...types import FetchedTranscript, TranscriptSegment
from ..base import ProviderError, VideoUnavailable
from . import _kevent

log = logging.getLogger(__name__)

_YDL_AUDIO_OPTS: dict = {
    "quiet": True, "no_warnings": True,
    # Prend le meilleur audio (souvent opus/webm sur YouTube) et le
    # remuxe vers FLAC. Raison : Kevent gateway refuse .webm
    # ("extension '.webm' not accepted") + bug pyannote samples
    # mismatch sur AAC (issue #41 fixée Mirai-side via FLAC). FLAC =
    # lossless après décodage opus (pas de perte additionnelle) et
    # accepté par tous les backends (Whisper, pyannote, vm-direct).
    "format": "bestaudio/best",
    "noplaylist": True,
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "flac",
    }],
}


def fetch_audio_bytes(video_id: str) -> tuple[bytes, str]:
    """Télécharge l'audio en FLAC, retourne (bytes, basename).

    Pas d'appel Kevent ici — destiné au chemin Phase B où video-ingest
    déporte l'audio vers internal-ingester qui pilote le pipeline
    standard (Whisper + pyannote + LLM). Le caller (orchestrator)
    POSTe les bytes vers materialize-audio.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory(prefix="video-ingest-audio-") as tmp:
        opts = dict(_YDL_AUDIO_OPTS)
        opts["outtmpl"] = str(Path(tmp) / "%(id)s.%(ext)s")
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
        except DownloadError as e:
            msg = str(e).lower()
            if any(k in msg for k in ("private", "removed", "unavailable", "blocked")):
                raise VideoUnavailable(str(e)) from e
            raise ProviderError(f"yt-dlp download failed: {e}") from e
        # FFmpegExtractAudio postprocess écrit le .flac et garde aussi
        # le fichier source — on prend le .flac.
        flacs = list(Path(tmp).glob("*.flac"))
        if not flacs:
            raise ProviderError("yt-dlp postprocess FLAC n'a rien produit")
        audio_path = flacs[0]
        return audio_path.read_bytes(), audio_path.name


def fetch_audio_and_transcribe(video_id: str, language: str = "fr") -> FetchedTranscript:
    """Télécharge l'audio, le transmet à Kevent, renvoie le transcript.

    Le fichier audio est dans un `TemporaryDirectory` détruit à la sortie.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory(prefix="video-ingest-") as tmp:
        opts = dict(_YDL_AUDIO_OPTS)
        opts["outtmpl"] = str(Path(tmp) / "%(id)s.%(ext)s")
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
        except DownloadError as e:
            msg = str(e).lower()
            if any(k in msg for k in ("private", "removed", "unavailable", "blocked")):
                raise VideoUnavailable(str(e)) from e
            raise ProviderError(f"yt-dlp download failed: {e}") from e

        files = list(Path(tmp).iterdir())
        if len(files) != 1:
            raise ProviderError(
                f"yt-dlp a produit {len(files)} fichier(s) au lieu de 1 : {files}"
            )
        audio_path = files[0]
        audio_bytes = audio_path.read_bytes()
        log.info("audio téléchargé: %s (%d bytes)", audio_path.name, len(audio_bytes))

        # Appel Kevent dans le scope du TemporaryDirectory : si Kevent
        # échoue, l'audio reste éphémère.
        result = _kevent.submit_transcription(
            audio_bytes, audio_path.name, language=language,
        )
        if isinstance(result, str):
            # submit a retourné un job_id → on poll
            payload = _kevent.wait_for_result(result)
        else:
            payload = result
    # → sortie du `with` : audio supprimé du filesystem (TemporaryDirectory).

    return _map_kevent_payload(payload, language=language)


def _map_kevent_payload(payload: dict, *, language: str) -> FetchedTranscript:
    """Convertit la réponse Kevent (format verbose_json Whisper) en
    `FetchedTranscript`. Tolère plusieurs schémas (segments, words, text).
    """
    segments_raw = payload.get("segments") or []
    if segments_raw:
        segments = [
            TranscriptSegment(
                text=s.get("text", "").strip(),
                start_seconds=float(s.get("start", 0.0)),
                duration_seconds=float(s.get("end", 0.0)) - float(s.get("start", 0.0)),
            )
            for s in segments_raw if s.get("text")
        ]
    else:
        # Fallback : un seul segment avec tout le texte (pas d'horodatage).
        text = payload.get("text", "")
        segments = [TranscriptSegment(text=text, start_seconds=0.0, duration_seconds=0.0)] if text else []

    if not segments:
        raise ProviderError("Kevent a renvoyé un transcript vide")

    detected_lang = payload.get("language") or language
    return FetchedTranscript(
        language=detected_lang,
        method="asr_whisper_kevent",
        segments=segments,
    )
