"""Tests du modèle UserAudioFile enrichi par la migration 022.

Couvre :
- la colonne source_type existe et default sur 'upload'
- la colonne external_video_source_id existe (BigInteger, nullable)
- stored_filename est nullable côté modèle
- les nouvelles valeurs ENUM acceptées (youtube_subtitle, youtube_audio)
"""

from libs.shared.app.models import UserAudioFile


def test_source_type_column_present():
    cols = {c.name: c for c in UserAudioFile.__table__.columns}
    assert "source_type" in cols, "Colonne source_type absente"
    assert cols["source_type"].default.arg == "upload"


def test_external_video_source_id_present_nullable():
    cols = {c.name: c for c in UserAudioFile.__table__.columns}
    assert "external_video_source_id" in cols
    assert cols["external_video_source_id"].nullable is True
    # Indexed (partial WHERE NOT NULL côté DB ; côté modèle SQLA on a juste l'index)
    assert cols["external_video_source_id"].index is True


def test_stored_filename_now_nullable():
    cols = {c.name: c for c in UserAudioFile.__table__.columns}
    # Migration 022 a fait DROP NOT NULL — le modèle suit.
    assert cols["stored_filename"].nullable is True, (
        "stored_filename doit être nullable depuis la migration 022 "
        "(les rows external sans S3 file)"
    )


def test_can_construct_with_youtube_subtitle_source_type():
    """Vérifie qu'on peut construire une instance avec les nouvelles valeurs."""
    uaf = UserAudioFile(
        user_sub="u-test",
        original_session_code="abcde12345",
        original_filename="youtube-mensch.txt",
        stored_filename=None,  # OK depuis 022
        file_size_bytes=0,
        source_type="youtube_subtitle",
        external_video_source_id=42,
    )
    assert uaf.source_type == "youtube_subtitle"
    assert uaf.external_video_source_id == 42
    assert uaf.stored_filename is None
    # Defaults
    assert uaf.origin is None or uaf.origin == "upload"


def test_can_construct_with_youtube_audio_source_type():
    uaf = UserAudioFile(
        user_sub="u-test",
        original_session_code="abcde12345",
        original_filename="x.m4a",
        stored_filename="stored.m4a",
        file_size_bytes=12345,
        source_type="youtube_audio",
        external_video_source_id=99,
    )
    assert uaf.source_type == "youtube_audio"


def test_default_source_type_is_upload():
    """Construire SANS préciser source_type → l'attribut Python est None
    (le DEFAULT s'applique côté DB à l'INSERT, pas en Python)."""
    uaf = UserAudioFile(
        user_sub="u-test",
        original_session_code="abcde12345",
        original_filename="audio.m4a",
        stored_filename="stored.m4a",
        file_size_bytes=1024,
    )
    # source_type n'est pas explicitement posé → reste None côté Python
    # (le DEFAULT 'upload' s'applique au moment du flush DB).
    # On vérifie que la colonne a bien default='upload' au niveau de la définition.
    col = UserAudioFile.__table__.columns["source_type"]
    assert col.default.arg == "upload"
