"""Non-régression incident anti-bot YouTube du 2026-08-02.

Les deux `video_id` utilisés ici sont ceux qui ont réellement échoué en
prod-bêta (jobs 31 et 32), et le message d'erreur est copié tel quel des
logs du worker — apostrophe typographique U+2019 comprise, qui est
précisément ce qui faisait rater une détection naïve en `"you're" in msg`.

Séquence observée en prod, qui prouve le caractère transitoire :

    09:59:43  job 31  UsMDkEsR-ok   → anti-bot        ❌
    10:03:53  job 32  o-zkvb0iFDQ   → anti-bot        ❌
    10:03:54  job 33  UsMDkEsR-ok   → materialize OK  ✅   (même vidéo)

Zéro réseau : yt-dlp, oEmbed et la BDD sont mockés.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.video_ingest.app import orchestrator
from services.video_ingest.app.jobs import Job
from services.video_ingest.app.providers.base import (
    ProviderError,
    TransientProviderError,
    VideoUnavailable,
)
from services.video_ingest.app.providers.youtube import _errors
from services.video_ingest.app.providers.youtube import metadata as md
from services.video_ingest.app.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)

# --- Vidéos réellement bloquées en prod (jobs 31 et 32) ---------------------
VID_BLOQUEE_1 = "UsMDkEsR-ok"   # Generative UI for any agent — Google Cloud Tech
VID_BLOQUEE_2 = "o-zkvb0iFDQ"   # MCP UI: Extending the frontier — AI Engineer

# Message brut du worker, apostrophe U+2019 incluse.
MSG_ANTIBOT = (
    "ERROR: [youtube] {vid}: Sign in to confirm you’re not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication. "
    "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
)


# ===========================================================================
# 1. Classification des erreurs
# ===========================================================================

@pytest.mark.parametrize("vid", [VID_BLOQUEE_1, VID_BLOQUEE_2])
def test_message_antibot_prod_est_classe_transitoire(vid):
    """Le message exact des jobs 31/32 doit être retryable, pas terminal."""
    err = _errors.classify(MSG_ANTIBOT.format(vid=vid))
    assert isinstance(err, TransientProviderError)


def test_apostrophe_typographique_ne_casse_pas_la_detection():
    """U+2019 vs U+0027 : la normalisation doit rendre les deux équivalents."""
    assert _errors.is_transient("Sign in to confirm you’re not a bot.")
    assert _errors.is_transient("Sign in to confirm you're not a bot.")


@pytest.mark.parametrize("msg", [
    "ERROR: unable to download video subtitles for 'fr': HTTP Error 429: Too Many Requests",
    "HTTP Error 503: Service Unavailable",
    "RequestBlocked: YouTube is blocking requests from your IP",
    "IpBlocked: too many requests from this IP",
    "The read operation timed out",
])
def test_autres_erreurs_transitoires(msg):
    assert isinstance(_errors.classify(msg), TransientProviderError)


@pytest.mark.parametrize("msg", [
    "ERROR: [youtube] xyz: Private video. Sign in if you've been granted access",
    "ERROR: [youtube] xyz: Video unavailable. This video has been removed",
    "ERROR: [youtube] xyz: This video is not available in your country (blocked)",
])
def test_erreurs_terminales_restent_terminales(msg):
    """Non-régression : le comportement V1 sur les vidéos mortes ne bouge pas."""
    assert isinstance(_errors.classify(msg), VideoUnavailable)


def test_erreur_inconnue_reste_provider_error():
    err = _errors.classify("ERROR: something completely unexpected happened")
    assert type(err) is ProviderError


def test_prefix_est_conserve_dans_le_message():
    err = _errors.classify("HTTP Error 429", prefix="yt-dlp download failed")
    assert str(err).startswith("yt-dlp download failed: ")


# ===========================================================================
# 2. Fallback oEmbed sur les métadonnées
# ===========================================================================

def _ydl_raising(msg: str):
    from yt_dlp.utils import DownloadError
    ydl = MagicMock()
    ydl.extract_info.side_effect = DownloadError(msg)
    cm = MagicMock()
    cm.__enter__.return_value = ydl
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


UsMD = VID_BLOQUEE_1
OZKV = VID_BLOQUEE_2

# Réponses oEmbed réelles (relevées pendant l'incident).
_OEMBED = {
    UsMD: {
        "title": "Generative UI for any agent, anywhere: A2UI, AG-UI, MCP Apps, and more",
        "author_name": "Google Cloud Tech",
    },
    OZKV: {
        "title": "MCP UI: Extending the frontier",
        "author_name": "AI Engineer",
    },
}


def _oembed_payload(vid):
    return _OEMBED[vid]


@pytest.mark.parametrize("vid", [UsMD, OZKV])
def test_antibot_bascule_sur_oembed(vid, monkeypatch):
    """yt-dlp bloqué → on sert quand même titre + chaîne via oEmbed.

    C'est ce qui évite qu'un import de SOUS-TITRES échoue à cause des
    métadonnées : `youtube-transcript-api` tape un autre endpoint et
    reste accessible.
    """
    monkeypatch.delenv("VIDEO_INGEST_OEMBED_FALLBACK", raising=False)
    with patch.object(md, "YoutubeDL", _ydl_raising(MSG_ANTIBOT.format(vid=vid))), \
         patch.object(md, "_fetch_oembed", return_value=VideoMetadata(
             provider="youtube", provider_video_id=vid,
             canonical_url=f"https://www.youtube.com/watch?v={vid}",
             title=_oembed_payload(vid)["title"],
             channel=_oembed_payload(vid)["author_name"],
             duration_sec=None,
             extra={"metadata_source": "oembed", "metadata_degraded": True},
         )):
        result = md.fetch(vid)

    assert result.provider_video_id == vid
    assert result.title == _oembed_payload(vid)["title"]
    assert result.channel == _oembed_payload(vid)["author_name"]
    assert result.duration_sec is None          # oEmbed n'expose pas la durée
    assert result.extra["metadata_degraded"] is True


def test_antibot_et_oembed_ko_leve_transient(monkeypatch):
    """Si oEmbed tombe aussi, on remonte la cause racine — retryable."""
    monkeypatch.delenv("VIDEO_INGEST_OEMBED_FALLBACK", raising=False)
    with patch.object(md, "YoutubeDL", _ydl_raising(MSG_ANTIBOT.format(vid=UsMD))), \
         patch.object(md, "_fetch_oembed", return_value=None):
        with pytest.raises(TransientProviderError):
            md.fetch(UsMD)


def test_kill_switch_oembed(monkeypatch):
    """`VIDEO_INGEST_OEMBED_FALLBACK=0` désactive le fallback."""
    monkeypatch.setenv("VIDEO_INGEST_OEMBED_FALLBACK", "0")
    oembed = MagicMock()
    with patch.object(md, "YoutubeDL", _ydl_raising(MSG_ANTIBOT.format(vid=UsMD))), \
         patch.object(md, "_fetch_oembed", oembed):
        with pytest.raises(TransientProviderError):
            md.fetch(UsMD)
    oembed.assert_not_called()


def test_video_morte_ne_declenche_pas_oembed():
    """Non-régression : pas de fallback sur une erreur terminale."""
    oembed = MagicMock()
    with patch.object(md, "YoutubeDL", _ydl_raising("ERROR: Private video")), \
         patch.object(md, "_fetch_oembed", oembed):
        with pytest.raises(VideoUnavailable):
            md.fetch(UsMD)
    oembed.assert_not_called()


# ===========================================================================
# 3. Backoff dans l'orchestrateur
# ===========================================================================

def _conn():
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def _job(**over):
    base = dict(
        id=31, url=f"https://www.youtube.com/watch?v={UsMD}", user_sub="u-1",
        context="meeting", context_id="m-1", language_pref="en",
        force_audio=False, attempts=1,
    )
    base.update(over)
    return Job(**base)


def _provider_raising(exc):
    p = MagicMock()
    p.name = "youtube"
    p.matches_url.return_value = True
    p.parse_canonical_id.return_value = (UsMD, f"https://www.youtube.com/watch?v={UsMD}")
    p.fetch_metadata.side_effect = exc
    return p


def test_backoff_sequence():
    """20s → 60s → 180s : cumul ~4min20, calé sur le délai de reprise
    réellement observé en prod (échec 09:59, succès 10:03)."""
    assert orchestrator.retry_delay_seconds(1) == 20
    assert orchestrator.retry_delay_seconds(2) == 60
    assert orchestrator.retry_delay_seconds(3) == 180


@pytest.mark.parametrize("attempts,delai", [(1, 20), (2, 60), (3, 180)])
def test_antibot_declenche_un_retry_pas_un_fail(attempts, delai):
    """LE correctif : l'anti-bot ne doit plus tuer le job."""
    conn = _conn()
    prov = _provider_raising(TransientProviderError(MSG_ANTIBOT.format(vid=UsMD)))
    with patch.object(orchestrator, "jobs_mod") as jm, \
         patch.object(orchestrator, "repo") as rp:
        rp.find_source_by_provider_id.return_value = None
        orchestrator.run_and_record(conn, [prov], _job(attempts=attempts))

    jm.fail.assert_not_called()
    jm.retry.assert_called_once()
    assert jm.retry.call_args.kwargs["delay_seconds"] == delai


def test_abandon_apres_max_tentatives():
    """Le retry n'est pas infini : au bout de MAX_ATTEMPTS on marque failed."""
    conn = _conn()
    prov = _provider_raising(TransientProviderError(MSG_ANTIBOT.format(vid=OZKV)))
    with patch.object(orchestrator, "jobs_mod") as jm, \
         patch.object(orchestrator, "repo") as rp:
        rp.find_source_by_provider_id.return_value = None
        orchestrator.run_and_record(conn, [prov], _job(attempts=orchestrator.MAX_ATTEMPTS))

    jm.retry.assert_not_called()
    jm.fail.assert_called_once()
    assert "abandon après" in jm.fail.call_args.kwargs["error"]


def test_video_morte_ne_retente_pas():
    """Non-régression : `VideoUnavailable` reste terminal (retry inutile)."""
    conn = _conn()
    prov = _provider_raising(VideoUnavailable("Private video"))
    with patch.object(orchestrator, "jobs_mod") as jm, \
         patch.object(orchestrator, "repo") as rp:
        rp.find_source_by_provider_id.return_value = None
        orchestrator.run_and_record(conn, [prov], _job())

    jm.retry.assert_not_called()
    jm.fail.assert_called_once()
    assert jm.fail.call_args.kwargs["error"].startswith("video_unavailable")


def test_provider_error_generique_reste_terminal():
    """Non-régression : seule la classe transitoire est rejouée."""
    conn = _conn()
    prov = _provider_raising(ProviderError("boom inattendu"))
    with patch.object(orchestrator, "jobs_mod") as jm, \
         patch.object(orchestrator, "repo") as rp:
        rp.find_source_by_provider_id.return_value = None
        orchestrator.run_and_record(conn, [prov], _job())

    jm.retry.assert_not_called()
    jm.fail.assert_called_once()
    assert jm.fail.call_args.kwargs["error"].startswith("provider_error")


# ===========================================================================
# 4. Durée dérivée des sous-titres quand oEmbed a servi les métadonnées
# ===========================================================================

def test_duree_derivee_des_soustitres():
    """oEmbed ne donne pas la durée → on la déduit du dernier segment.

    Valeurs calées sur les sous-titres réels de `UsMDkEsR-ok` téléchargés
    pendant l'incident : dernier segment à 2444.0s + 3.0s ≈ 2447s
    (yt-dlp annonce 2456s — l'écart est le générique de fin, sans sous-titre).
    """
    conn = _conn()
    prov = MagicMock()
    prov.name = "youtube"
    prov.matches_url.return_value = True
    prov.parse_canonical_id.return_value = (UsMD, f"https://www.youtube.com/watch?v={UsMD}")
    prov.fetch_metadata.return_value = VideoMetadata(
        provider="youtube", provider_video_id=UsMD,
        canonical_url=f"https://www.youtube.com/watch?v={UsMD}",
        title="Generative UI for any agent", channel="Google Cloud Tech",
        duration_sec=None,  # ← métadonnées dégradées oEmbed
        extra={"metadata_source": "oembed"},
    )
    prov.fetch_subtitles.return_value = FetchedTranscript(
        language="en", method="subtitle_manual",
        segments=[
            TranscriptSegment("intro", 0.0, 3.0),
            TranscriptSegment("fin", 2444.0, 3.0),
        ],
    )

    with patch.object(orchestrator, "repo") as rp, \
         patch.object(orchestrator, "_notify_materialize"):
        rp.find_source_by_provider_id.return_value = None
        rp.upsert_source.return_value = 99
        orchestrator.run_job(conn, [prov], _job())

    rp.update_source_duration.assert_called_once_with(conn, 99, 2447)


def test_duree_exacte_non_ecrasee():
    """Si yt-dlp a donné la durée, on n'estime rien."""
    conn = _conn()
    prov = MagicMock()
    prov.name = "youtube"
    prov.matches_url.return_value = True
    prov.parse_canonical_id.return_value = (UsMD, f"https://www.youtube.com/watch?v={UsMD}")
    prov.fetch_metadata.return_value = VideoMetadata(
        provider="youtube", provider_video_id=UsMD,
        canonical_url=f"https://www.youtube.com/watch?v={UsMD}",
        title="x", channel="y", duration_sec=2456,
    )
    prov.fetch_subtitles.return_value = FetchedTranscript(
        language="en", method="subtitle_manual",
        segments=[TranscriptSegment("fin", 2444.0, 3.0)],
    )

    with patch.object(orchestrator, "repo") as rp, \
         patch.object(orchestrator, "_notify_materialize"):
        rp.find_source_by_provider_id.return_value = None
        rp.upsert_source.return_value = 99
        orchestrator.run_job(conn, [prov], _job())

    rp.update_source_duration.assert_not_called()
