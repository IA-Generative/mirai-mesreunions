"""Local preview server for the mydevices page.

Boots a tiny Flask app on http://127.0.0.1:5555 that renders the mydevices
template from `services/mesreunions-web/app/templates/index.html` with mock
data, and stubs the half-dozen API endpoints the page hits. Auth/DB/S3/
Keycloak are bypassed.

Why: iterating on UX in the real service requires a Docker image rebuild +
push + k8s rollout (minutes). Here every edit to the template is picked up
on browser refresh (file is re-read from disk on each render).

Run:
    python tools/preview_mydevices.py
    open http://127.0.0.1:5555

Edit `services/mesreunions-web/app/templates/index.html`, reload the
browser, see the change. Tweak `MOCK_SESSIONS` below to exercise edge cases
(many files, failed transcription, partial outputs, etc.).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, render_template_string, request, abort

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_TEMPLATE_PATH = (
    REPO_ROOT / "services" / "mesreunions-web" / "app" / "templates" / "index.html"
)


def load_template() -> str:
    """Re-read the mydevices template from disk on every render (hot reload)."""
    return INDEX_TEMPLATE_PATH.read_text(encoding="utf-8")


# ─── Mock data ─────────────────────────────────────────────────────────────

# Données fidèles à l'état prod-bêta au 2026-05-11 (export depuis
# postgres-external) — 1 session 9NXBMN avec 2 fichiers transférés
# (un poème court 73s + un échange IA d'~1h45). Kevent partiellement
# complété (les outputs sont des suppositions plausibles : transcript
# brut OK, étapes LLM avancées partiellement absentes).
USER = {
    "sub": "ff3bda0f-86ed-4a7f-8377-da078cbf1b01",
    "name": "User One",
    "email": "user1@test.local",
}

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

# Anchor sur created_at de la session prod pour que les "il y a Xh" matchent.
SESSION_CREATED = datetime.fromisoformat("2026-05-11T21:12:54.530637+00:00")

FILE_1_ID = "98ae0a98-68a9-46ce-b223-8e54a15f1ea0"
FILE_2_ID = "1a417061-b038-40bd-827d-3adaa13d4851"
SESSION_ID = "f131c325-4e8d-4ba1-b3ef-dcf8471fa9c6"

def _file_urls(fid):
    return {
        "download_url": f"/api/file/download/{fid}",
        "stream_url": f"/api/file/stream/{fid}",
        "source_download_url": f"/api/file/download-source/{fid}",
        "source_stream_url": f"/api/file/stream-source/{fid}",
        "transcoded_download_url": f"/api/file/download-transcoded/{fid}",
        "transcoded_stream_url": f"/api/file/stream-transcoded/{fid}",
        "transferred_download_url": f"/api/file/download-transferred/{fid}",
        "transferred_stream_url": f"/api/file/stream-transferred/{fid}",
        "impact_url": f"/api/file/normalization-impact/{fid}",
    }

MOCK_SESSIONS = [
    {
        "id": SESSION_ID,
        "simple_code": "9NXBMN",
        "qr_token": "MOCK-QR-TOKEN-synthetic-preview-0000000000",
        "status": "active",
        "upload_count": 2,
        "max_uploads": 50,
        # Prod : QR expiré (5min TTL passées) mais device encore en rétention.
        "expires_at": "2026-05-11T21:17:54.518179+00:00",
        "created_at": "2026-05-11T21:12:54.530637+00:00",
        # → lifecycle "enrolled" tant que le device existe (cf. MOCK_DEVICES).
        "lifecycle_state": "enrolled",
        "uploads": [
            {
                "id": FILE_1_ID,
                "original_filename": "poemes013_chameaubatonsflottants_chj_128kb.mp3",
                "status": "transferred",
                "status_message": "Pipeline Kevent kevent_partially_completed",
                "audio_quality_score": 3,
                "audio_duration_seconds": 73.456327,
                "created_at": "2026-05-11T21:13:25.030605+00:00",
                "updated_at": "2026-05-11T21:14:35.282952+00:00",
                "source_available": True,
                "transcoded_available": True,
                "transferred_available": True,
                **_file_urls(FILE_1_ID),
            },
            {
                "id": FILE_2_ID,
                "original_filename": "Echange IA.m4a",
                "status": "transferred",
                "status_message": "Pipeline Kevent kevent_partially_completed",
                "audio_quality_score": 4.5,
                "audio_duration_seconds": 6303.146667,
                "created_at": "2026-05-11T21:20:13.491133+00:00",
                "updated_at": "2026-05-11T21:44:37.360360+00:00",
                "source_available": True,
                "transcoded_available": True,
                "transferred_available": True,
                **_file_urls(FILE_2_ID),
            },
        ],
    },
]

MOCK_DEVICES = [
    {
        "device_id": "dev-9nxbmn",
        "device_name": "Mobile (test)",
        "name": "Mobile (test)",
        "qr_token": "MOCK-QR-TOKEN-synthetic-preview-0000000000",
        "status": "active",
        "created_at": "2026-05-11T21:13:25.030605+00:00",
        "last_seen_at": "2026-05-11T21:44:37.360360+00:00",
        # Rétention 15j depuis l'enrôlement (DEVICE_TOKEN_RETENTION_HOURS=360 prod)
        "retention_expires_at": "2026-05-26T21:13:25.030605+00:00",
        "upload_count": 2,
    },
]

# Transcripts plausibles : Kevent partiel = transcript brut OK,
# étapes LLM avancées partiellement absentes. Suggested_filename + résumé
# fabriqués pour exercer la vue détail.
MOCK_TRANSCRIPTS = {
    FILE_1_ID: {
        "available": True,
        "transcription_status": "kevent_partially_completed",
        "transcription_engine": "kevent",
        "transcription_language": "fr",
        "outputs": {
            "transcript": True,
            "transcript-tagged": False,  # diarisation a échoué (mono-locuteur, 73s)
            "transcript-corrected": True,
            "transcript-cleaned": True,
            "transcript-reformulated": False,
            "meeting-cr": False,  # CR non pertinent pour un poème court
        },
        "suggested_filename": "Poème — Le chameau aux bâtons flottants",
        "key_points_summary": (
            "• Lecture d'un poème court (≈1min13) : « Le chameau aux bâtons flottants ».\n"
            "• Mono-locuteur, ton narratif. Pas de structure de réunion.\n"
            "• Compte-rendu structuré non généré (contenu inadapté à l'analyse 5 sections)."
        ),
    },
    FILE_2_ID: {
        "available": True,
        # État réel exporté de prod-bêta. Pour exercer l'animation pulse
        # "en cours", flipper temporairement vers 'kevent_transcribing'
        # (+ tous les outputs à False sauf 'transcript').
        "transcription_status": "kevent_partially_completed",
        "transcription_engine": "kevent",
        "transcription_language": "fr",
        "outputs": {
            "transcript": True,
            "transcript-tagged": True,
            "transcript-corrected": True,
            "transcript-cleaned": True,
            "transcript-reformulated": True,
            "meeting-cr": True,
        },
        "suggested_filename": "Échange IA — Architecture multi-agents et orchestration",
        "key_points_summary": (
            "• Discussion technique de ~1h45 sur l'architecture d'un système multi-agents IA.\n"
            "• Sujets : orchestration, mémoire partagée entre agents, coût d'inférence.\n"
            "• Décision esquissée : prototyper une approche hub-and-spoke avant fin mai.\n"
            "• À approfondir : stratégie de fallback en cas de panne LLM upstream.\n"
            "• ⚠ Étape Compte-rendu structuré n'a pas abouti (timeout sur audio long)."
        ),
    },
}


# ─── Flask app ─────────────────────────────────────────────────────────────

app = Flask(
    __name__,
    static_folder=str(REPO_ROOT / "services" / "mesreunions-web" / "app" / "static"),
    static_url_path="/static",
)


@app.route("/")
def index():
    tpl = load_template()
    return render_template_string(
        tpl,
        user=USER,
        short_ttl_enabled=False,
        device_retention_days=15,
    )


@app.route("/api/my-sessions")
def api_my_sessions():
    return jsonify(MOCK_SESSIONS)


@app.route("/api/my-devices")
def api_my_devices():
    return jsonify(MOCK_DEVICES)


@app.route("/api/file/transcript-status/<file_id>")
def api_transcript_status(file_id):
    data = MOCK_TRANSCRIPTS.get(file_id)
    if data is None:
        return jsonify({"available": False, "reason": "not_ready"})
    return jsonify(data)


@app.route("/api/queue-status")
def api_queue_status():
    """Stub local du proxy queue-status. Renvoie un payload réaliste pour
    exercer la branche « N jobs en attente » du hint UI. Modifier les
    valeurs ci-dessous pour tester d'autres scénarios :
      - your_position=1 → "⏳ En tête de file"
      - your_position=3, pending_total=5 → "Position 3/5 — env. 1 min 30 s"
      - pending_total=0, processing_total=1 → "⏳ Tour suivant"
      - pending_total=0, processing_total=0 → "⏳ Réservation de la file…"
    """
    return jsonify({
        "pending_total": 2,
        "processing_total": 1,
        "your_position": None,    # passe à un nombre quand kevent_job_id sera persisté (Phase 2)
        "eta_seconds": None,
        "throughput_per_min": 0.4,
        "stale": False,
        "fetched_at": _iso(datetime.now(timezone.utc)),
    })


@app.route("/api/file/normalization-impact/<file_id>")
def api_normalization_impact(file_id):
    return jsonify({
        "available": True,
        "summary": "Mock: source −22.3 LUFS / TP −5.4 dB → cible −16.0 LUFS / TP −1.5 dB (gain +6.3 dB)",
    })


# Stub endpoints: anything that mutates returns a generic 200 ok.
@app.route("/api/file/<file_id>", methods=["DELETE"])
def api_delete_file(file_id):
    for sess in MOCK_SESSIONS:
        sess["uploads"] = [u for u in sess["uploads"] if u["id"] != file_id]
    return jsonify({"ok": True})


@app.route("/api/file/<file_id>/rename", methods=["POST"])
def api_rename_file(file_id):
    return jsonify({"ok": True})


@app.route("/api/my-sessions/<simple_code>", methods=["DELETE"])
def api_delete_session(simple_code):
    global MOCK_SESSIONS
    MOCK_SESSIONS = [s for s in MOCK_SESSIONS if s["simple_code"] != simple_code]
    return jsonify({"ok": True})


@app.route("/api/purge-my-sessions", methods=["POST"])
def api_purge_my_sessions():
    global MOCK_SESSIONS
    MOCK_SESSIONS = []
    return jsonify({"ok": True, "purged": 2})


@app.route("/api/file/download/<path:_rest>")
@app.route("/api/file/stream/<path:_rest>")
@app.route("/api/file/download-source/<path:_rest>")
@app.route("/api/file/stream-source/<path:_rest>")
@app.route("/api/file/download-transcoded/<path:_rest>")
@app.route("/api/file/stream-transcoded/<path:_rest>")
@app.route("/api/file/download-transferred/<path:_rest>")
@app.route("/api/file/stream-transferred/<path:_rest>")
@app.route("/api/file/transcript/<path:_rest>")
@app.route("/api/file/meeting-cr/<path:_rest>")
def api_download_stub(_rest):
    return (b"<mock content>", 200, {"Content-Type": "text/plain"})


@app.route("/logout")
def logout():
    return "(preview) logout — restart the preview server to reset state."


if __name__ == "__main__":
    print(f"📄 Reading template from: {INDEX_TEMPLATE_PATH}")
    print("🚀 Preview server → http://127.0.0.1:5555")
    print("✏️  Edit services/mesreunions-web/app/main.py and reload the browser.")
    app.run(host="127.0.0.1", port=5555, debug=True)
