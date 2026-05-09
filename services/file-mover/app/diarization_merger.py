"""
Pure functions to merge a Whisper verbose_json transcription with a
pyannote diarization output, producing speaker-tagged Markdown.

The Whisper ``verbose_json`` response gives us per-segment timestamps
(coarse, ~30s segments by default), and pyannote returns per-speaker
intervals. We assign each transcription segment to the speaker whose
diarisation interval covers the largest overlap with that segment.

The output is a Markdown document of the form::

    **SPEAKER_00** _(0:00 → 0:14)_
    > Bonjour à tous, on commence la réunion.

    **SPEAKER_01** _(0:14 → 0:32)_
    > Merci. Je vais présenter le sujet.

If diarisation is empty (one speaker, or pyannote returned nothing), the
function falls back to a single-speaker block tagged ``SPEAKER_00``.

Speaker labels are stable across the document — calling code can later
substitute ``SPEAKER_NN`` with real names without re-running this merge.

This is a **pure function** so it can be unit-tested without any LLM,
HTTP, or DB.
"""

from __future__ import annotations

from typing import Dict, List


def _format_time(seconds: float) -> str:
    """Format ``seconds`` as ``M:SS`` (or ``H:MM:SS`` over an hour)."""
    s = int(round(seconds))
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Return the duration of overlap between two intervals (0 if disjoint)."""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _assign_speaker(segment_start: float, segment_end: float, diar_segments: List[dict]) -> str:
    """Pick the speaker whose interval covers the largest overlap with the segment."""
    if not diar_segments:
        return "SPEAKER_00"
    best_speaker = "SPEAKER_00"
    best_overlap = 0.0
    for d in diar_segments:
        try:
            d_start = float(d.get("start", 0.0))
            d_end = float(d.get("end", 0.0))
            speaker = str(d.get("speaker") or "SPEAKER_00")
        except (TypeError, ValueError):
            continue
        ov = _overlap(segment_start, segment_end, d_start, d_end)
        if ov > best_overlap:
            best_overlap = ov
            best_speaker = speaker
    return best_speaker


def merge_to_markdown(
    transcription: dict,
    diarization: dict,
    speaker_names: Dict[str, str] | None = None,
) -> str:
    """
    Merge a Whisper ``verbose_json`` transcription with a pyannote
    diarization. Optional ``speaker_names`` maps ``SPEAKER_NN`` to real
    names; missing keys keep the raw label.
    """
    speaker_names = speaker_names or {}
    transcription_segments = transcription.get("segments") or []
    diarization_segments = diarization.get("segments") or []

    # Fallback when transcription has no segment-level timestamps (plain
    # ``json`` format instead of ``verbose_json``). We emit a single block
    # tagged with the dominant speaker if any, else SPEAKER_00.
    if not transcription_segments:
        text = (transcription.get("text") or "").strip()
        if not text:
            return ""
        speaker_raw = (
            diarization_segments[0].get("speaker", "SPEAKER_00")
            if diarization_segments
            else "SPEAKER_00"
        )
        speaker = speaker_names.get(speaker_raw, speaker_raw)
        duration = float(transcription.get("duration", 0.0))
        return f"**{speaker}** _(0:00 → {_format_time(duration)})_\n> {text}\n"

    # Build assignments per Whisper segment.
    blocks: List[str] = []
    current_speaker_raw: str | None = None
    current_start: float = 0.0
    current_text: List[str] = []
    current_end: float = 0.0

    def _flush() -> None:
        if current_speaker_raw is None or not current_text:
            return
        speaker = speaker_names.get(current_speaker_raw, current_speaker_raw)
        text = " ".join(s.strip() for s in current_text if s.strip())
        blocks.append(
            f"**{speaker}** _({_format_time(current_start)} → {_format_time(current_end)})_\n> {text}"
        )

    for seg in transcription_segments:
        try:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", start))
            text = str(seg.get("text", "")).strip()
        except (TypeError, ValueError):
            continue
        if not text:
            continue
        speaker_raw = _assign_speaker(start, end, diarization_segments)
        if speaker_raw != current_speaker_raw:
            _flush()
            current_speaker_raw = speaker_raw
            current_start = start
            current_text = [text]
            current_end = end
        else:
            current_text.append(text)
            current_end = end
    _flush()
    return "\n\n".join(blocks) + "\n" if blocks else ""


def list_unique_speakers(diarization: dict) -> List[str]:
    """Return the unique SPEAKER_NN labels in a diarization, in first-seen order."""
    seen: List[str] = []
    for d in diarization.get("segments") or []:
        s = str(d.get("speaker") or "")
        if s and s not in seen:
            seen.append(s)
    return seen
