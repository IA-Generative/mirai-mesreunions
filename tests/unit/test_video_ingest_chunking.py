"""Tests du chunking 60-90s avec chevauchement 15s (Principe 8)."""

import pytest

from services.video_ingest.app.chunking import chunk
from services.video_ingest.app.types import TranscriptSegment


def _seg(start, duration, text="x"):
    return TranscriptSegment(text=text, start_seconds=start, duration_seconds=duration)


def test_empty_input_returns_empty():
    assert chunk([]) == []


def test_short_transcript_fits_in_one_chunk():
    segs = [_seg(0, 5, "a"), _seg(5, 5, "b"), _seg(10, 5, "c")]
    chunks = chunk(segs, target_seconds=75, overlap_seconds=15)
    assert len(chunks) == 1
    assert chunks[0]["start_seconds"] == 0
    assert chunks[0]["end_seconds"] == 15
    assert chunks[0]["text"] == "a b c"


def test_chunks_respect_target_duration():
    # 20 segments de 5s = 100s total → doit produire >= 2 chunks à target=75
    segs = [_seg(i * 5, 5, f"s{i}") for i in range(20)]
    chunks = chunk(segs, target_seconds=75, overlap_seconds=15)
    assert len(chunks) >= 2
    # Aucun chunk ne doit dépasser la cible (sauf le tout dernier segment
    # qui peut faire que le chunk final excède un peu — le test garde
    # une marge).
    for c in chunks[:-1]:
        assert (c["end_seconds"] - c["start_seconds"]) <= 80


def test_chunks_are_sorted_and_non_empty():
    segs = [_seg(i * 5, 5, f"s{i}") for i in range(30)]
    chunks = chunk(segs, target_seconds=75, overlap_seconds=15)
    for c in chunks:
        assert c["text"]
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev["start_seconds"] < nxt["start_seconds"]


def test_overlap_creates_segment_duplication_at_boundary():
    """Un segment chevauchant la frontière de chunk apparaît dans les deux."""
    segs = [_seg(i * 10, 10, f"w{i}") for i in range(12)]  # 0..120s
    chunks = chunk(segs, target_seconds=60, overlap_seconds=15)
    assert len(chunks) >= 2
    # Vérification : il existe au moins un mot dont les bornes temporelles
    # tombent dans deux chunks distincts (zone de chevauchement).
    boundary_end = chunks[0]["end_seconds"]
    second_start = chunks[1]["start_seconds"]
    assert second_start < boundary_end, (
        f"chevauchement attendu, mais chunk2 démarre à {second_start} "
        f"alors que chunk1 finit à {boundary_end}"
    )


def test_segments_with_unicode_text_preserved():
    segs = [_seg(0, 3, "Café ☕"), _seg(3, 3, "réunion 📅")]
    chunks = chunk(segs)
    assert "Café" in chunks[0]["text"]
    assert "réunion" in chunks[0]["text"]


def test_progress_guaranteed_on_pathological_input():
    """Segments tous très longs (>target) : on doit quand même avancer."""
    segs = [_seg(0, 200), _seg(200, 200), _seg(400, 200)]
    chunks = chunk(segs, target_seconds=60, overlap_seconds=15)
    assert len(chunks) == 3  # chaque segment seul dépasse target → un chunk par segment


def test_segments_arrive_unordered_are_sorted():
    segs = [_seg(20, 5, "c"), _seg(0, 5, "a"), _seg(10, 5, "b")]
    chunks = chunk(segs, target_seconds=75)
    assert chunks[0]["start_seconds"] == 0
    assert "a" in chunks[0]["text"]
    assert chunks[0]["text"].index("a") < chunks[0]["text"].index("b") < chunks[0]["text"].index("c")
