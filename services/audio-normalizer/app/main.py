"""
Transcode Worker
================
Consumes files from the transcode queue, applies:
- Audio format normalization (→ MP4/AAC 16kHz mono)
- Loudness normalization (EBU R128)
- Voice frequency filtering (80Hz-8kHz bandpass)
- Audio quality scoring (1-5)
Then stores in processed-staging S3 and enqueues for transfer.
"""

import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    load_ext_db, load_s3_upload, load_s3_processed, RabbitMQConfig,
    TRANSCODE_SAMPLE_RATE, TRANSCODE_CHANNELS, INTERNAL_API_TOKEN,
    LOUDNORM_AUTO_DECISION, LOUDNORM_RMS_THRESHOLD_DBFS,
    LOUDNORM_PROBE_OFFSETS_S, LOUDNORM_PROBE_DURATION_S,
)
from libs.shared.app.models import ExternalBase, UploadedFile, UploadSession, UploadStatus
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.s3_helper import download_fileobj, upload_fileobj, ensure_bucket
from libs.shared.app.queue_helper import (
    consume_queue, publish_message, declare_queues,
    QUEUE_TRANSCODE, QUEUE_FILE_READY, RabbitMQConfig,
)
from libs.shared.app.security import require_strong_shared_secret

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

db_cfg = load_ext_db()
s3_upload_cfg = load_s3_upload()
s3_processed_cfg = load_s3_processed()
rabbit_cfg = RabbitMQConfig()
ENABLE_LOUDNORM = os.getenv("ENABLE_LOUDNORM", "true").strip().lower() in {"1", "true", "yes", "on"}
POST_LOUDNORM_FILTER_CHAIN = os.getenv(
    "POST_LOUDNORM_FILTER_CHAIN",
    "highpass=f=80,lowpass=f=7000,alimiter=limit=0.95",
).strip()

UPLOAD_PORTAL_URL = os.getenv("UPLOAD_PORTAL_INTERNAL_URL", "http://mobile-upload-pwa:8081")


def notify_portal(session_obj, file_obj, status_msg):
    """Notify the upload portal of a status change."""
    import requests
    try:
        requests.post(f"{UPLOAD_PORTAL_URL}/api/notify-status", json={
            "qr_token": session_obj.qr_token if session_obj else None,
            "file_id": str(file_obj.id),
            "filename": file_obj.original_filename,
            "status": file_obj.status.value,
            "message": status_msg,
            "quality": file_obj.audio_quality_score,
        }, headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        }, timeout=5)
    except Exception as e:
        logger.warning("Failed to notify portal: %s", e)


def _measure_loudnorm_quiet(input_path: str) -> dict | None:
    """Run a loudnorm pass-1 analysis and return {i, tp, lra} or None on error.

    Best-effort : si ffmpeg échoue ou la sortie est inattendue, on renvoie
    None et l'appelant laisse les colonnes DB à NULL. On limite l'analyse à
    180s pour ne pas allonger inutilement le temps de transcode.
    """
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-nostats",
            "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
            "-i", input_path,
            "-t", "180",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            logger.warning("loudnorm-measure: ffmpeg returncode=%s on %s",
                           proc.returncode, input_path)
            return None
        blocks = re.findall(r"\{[\s\S]*?\}", proc.stderr or "")
        if not blocks:
            logger.warning("loudnorm-measure: no JSON block in stderr for %s",
                           input_path)
            return None
        data = json.loads(blocks[-1])
        return {
            "i":   float(data.get("input_i")),
            "tp":  float(data.get("input_tp")),
            "lra": float(data.get("input_lra")),
        }
    except Exception:
        logger.warning("loudnorm-measure failed for %s", input_path, exc_info=True)
        return None


def analyze_audio_quality(input_path: str) -> dict:
    """
    Analyze audio quality using FFmpeg's astats and silencedetect.
    Returns dict with duration, sample_rate, quality_score (1-5).
    """
    result = {"duration": 0, "sample_rate": 0, "quality_score": 3.0}

    try:
        # Get stream info
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", input_path
        ]
        probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        info = json.loads(probe.stdout)

        # Duration
        fmt = info.get("format", {})
        result["duration"] = float(fmt.get("duration", 0))

        # Sample rate from first audio stream
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "audio":
                result["sample_rate"] = int(stream.get("sample_rate", 0))
                break

        # Audio stats for quality scoring
        stats_cmd = [
            "ffmpeg", "-i", input_path, "-af",
            "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
            "-f", "null", "-"
        ]
        stats = subprocess.run(stats_cmd, capture_output=True, text=True, timeout=60)

        # Parse RMS levels from stderr
        rms_values = []
        for line in stats.stderr.split("\n"):
            if "RMS_level" in line and "=" in line:
                try:
                    val = float(line.split("=")[-1].strip())
                    if val > -100:  # skip silence
                        rms_values.append(val)
                except ValueError:
                    pass

        # Silence detection
        silence_cmd = [
            "ffmpeg", "-i", input_path, "-af",
            "silencedetect=n=-40dB:d=1", "-f", "null", "-"
        ]
        silence = subprocess.run(silence_cmd, capture_output=True, text=True, timeout=60)
        silence_duration = 0
        for line in silence.stderr.split("\n"):
            if "silence_duration" in line:
                try:
                    silence_duration += float(line.split(":")[-1].strip())
                except ValueError:
                    pass

        # Quality scoring algorithm
        score = 3.0

        # RMS level scoring (voice should be around -20 to -10 dB)
        if rms_values:
            avg_rms = sum(rms_values) / len(rms_values)
            if -25 <= avg_rms <= -8:
                score += 1.0  # Good level
            elif -35 <= avg_rms <= -5:
                score += 0.5  # Acceptable
            else:
                score -= 1.0  # Too quiet or clipping

        # Duration scoring
        if result["duration"] < 3:
            score -= 1.0  # Too short
        elif result["duration"] > 10:
            score += 0.5  # Reasonable length

        # Silence ratio scoring
        if result["duration"] > 0:
            silence_ratio = silence_duration / result["duration"]
            if silence_ratio > 0.7:
                score -= 1.5  # Mostly silence
            elif silence_ratio > 0.4:
                score -= 0.5

        # Sample rate bonus
        if result["sample_rate"] >= 44100:
            score += 0.5
        elif result["sample_rate"] < 8000:
            score -= 0.5

        result["quality_score"] = max(1.0, min(5.0, round(score, 1)))

    except Exception as e:
        logger.warning("Quality analysis failed: %s", e)

    return result


def measure_rms_dbfs(input_path: str, offset_s: float, duration_s: float) -> float | None:
    """Sample a small window with `ffmpeg astats`; return mean RMS in dBFS or None.

    Bench `bench/reports/SYNTHESE.md` shows that loudnorm gives 0 WER gain on
    audio that's already loud enough — so we probe before deciding.
    """
    cmd = [
        "ffmpeg", "-v", "error", "-ss", str(offset_s), "-t", str(duration_s),
        "-i", input_path, "-af", "astats=metadata=1:reset=1", "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"RMS level dB:\s*(-?\d+(?:\.\d+)?)", r.stderr)
    return float(m.group(1)) if m else None


def needs_loudnorm(input_path: str, file_duration_s: float) -> tuple[bool, dict]:
    """Decide whether to run the 2-pass loudnorm. Returns (needed, debug_info)."""
    try:
        offsets = [float(o) for o in LOUDNORM_PROBE_OFFSETS_S.split(",") if o.strip()]
    except ValueError:
        offsets = [60.0, 300.0]
    valid = [o for o in offsets if o + LOUDNORM_PROBE_DURATION_S <= file_duration_s]
    if not valid:
        valid = [0.0]  # very short file — sample from start
    levels = []
    for o in valid:
        rms = measure_rms_dbfs(input_path, o, LOUDNORM_PROBE_DURATION_S)
        if rms is not None:
            levels.append((o, rms))
    if not levels:
        return True, {"reason": "probe failed (no valid samples), defaulting to normalize"}
    max_rms = max(l[1] for l in levels)
    needed = max_rms < LOUDNORM_RMS_THRESHOLD_DBFS
    return needed, {
        "levels_dbfs": levels,
        "max_rms_dbfs": max_rms,
        "threshold_dbfs": LOUDNORM_RMS_THRESHOLD_DBFS,
        "decision": "loudnorm" if needed else "skip",
    }


def transcode_audio(input_path: str, output_path: str, file_duration_s: float = 0.0) -> tuple[bool, bool]:
    """
    Transcode audio with voice optimization:
    - Bandpass filter 80Hz-8kHz (voice frequencies)
    - Loudness normalization (EBU R128) — only when probe says it's needed
      (LOUDNORM_AUTO_DECISION=true) or unconditionally (ENABLE_LOUDNORM)
    - Mono, 16kHz sample rate
    - Output as MP4 (AAC)

    Returns (success, was_normalized) — was_normalized is False when the audio
    was loud enough and we skipped the 2-pass loudnorm.
    """
    # Borne de coût/temps du transcodage : un média malformé ne doit pas
    # consommer FFmpeg sans limite. Timeout configurable via env, défaut 300s.
    _ffmpeg_timeout_default = max(10, int(os.getenv("FFMPEG_TRANSCODE_TIMEOUT_SECONDS", "300")))

    def run_ffmpeg(cmd, timeout=None):
        return subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout if timeout is not None else _ffmpeg_timeout_default,
        )

    def ffmpeg_base_input():
        # More tolerant decoding to avoid bursts/artefacts on partially corrupt streams.
        return [
            "ffmpeg", "-y",
            "-fflags", "+discardcorrupt",
            "-err_detect", "ignore_err",
            "-i", input_path,
        ]

    def parse_loudnorm_json(stderr_text: str):
        blocks = re.findall(r"\{[\s\S]*?\}", stderr_text or "")
        if not blocks:
            return None
        try:
            return json.loads(blocks[-1])
        except json.JSONDecodeError:
            return None

    def post_chain() -> str:
        return POST_LOUDNORM_FILTER_CHAIN or "highpass=f=80,lowpass=f=7000,alimiter=limit=0.95"

    # Per-file decision: skip the 2-pass loudnorm if audio is already loud enough.
    # Falls back to the global ENABLE_LOUDNORM env when auto-decision is off.
    if LOUDNORM_AUTO_DECISION and file_duration_s > 0:
        probe_needed, probe_info = needs_loudnorm(input_path, file_duration_s)
        logger.info("Loudnorm auto-decision: needed=%s info=%s", probe_needed, probe_info)
        local_loudnorm = probe_needed
    else:
        local_loudnorm = ENABLE_LOUDNORM

    try:
        # No loudnorm mode: keep voice filters + limiter only.
        if not local_loudnorm:
            cmd = [
                *ffmpeg_base_input(),
                "-af", post_chain(),
                "-ar", str(TRANSCODE_SAMPLE_RATE),
                "-ac", str(TRANSCODE_CHANNELS),
                "-c:a", "aac",
                "-b:a", "64k",
                "-movflags", "+faststart",
                output_path,
            ]
            logger.info("Transcode command (no loudnorm)")
            result = run_ffmpeg(cmd)
            if result.returncode != 0:
                logger.error("FFmpeg error: %s", result.stderr[-500:])
                return False, False
            return True, False

        # Pass 1: analyze full, raw signal (no bandpass, no limiter).
        measure_af = "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json"
        measure_cmd = [
            *ffmpeg_base_input(),
            "-af", measure_af,
            "-f", "null", "-",
        ]
        measure = run_ffmpeg(measure_cmd, timeout=180)
        if measure.returncode != 0:
            logger.warning("Loudnorm pass1 failed, fallback to stable mode. err=%s", measure.stderr[-300:])
            raise RuntimeError("pass1 failed")

        stats = parse_loudnorm_json(measure.stderr)
        if not stats:
            logger.warning("Loudnorm pass1 stats missing, fallback to stable mode")
            raise RuntimeError("pass1 stats missing")
        required = ["input_i", "input_lra", "input_tp", "input_thresh", "target_offset"]
        for key in required:
            try:
                v = float(stats.get(key))
            except (TypeError, ValueError):
                logger.warning("Loudnorm pass1 invalid %s=%r, fallback to stable mode", key, stats.get(key))
                raise RuntimeError("pass1 invalid stats")
            if not math.isfinite(v):
                logger.warning("Loudnorm pass1 non-finite %s=%r, fallback to stable mode", key, stats.get(key))
                raise RuntimeError("pass1 non-finite stats")

        # Pass 2: apply the measured parameters with linear=true (constant gain behavior).
        loudnorm_2pass = (
            "loudnorm=I=-16:TP=-1.5:LRA=11:"
            f"measured_I={stats['input_i']}:"
            f"measured_LRA={stats['input_lra']}:"
            f"measured_TP={stats['input_tp']}:"
            f"measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:"
            "linear=true:print_format=summary"
        )
        final_af = f"{loudnorm_2pass},{post_chain()}"
        apply_cmd = [
            *ffmpeg_base_input(),
            "-af", final_af,
            "-ar", str(TRANSCODE_SAMPLE_RATE),
            "-ac", str(TRANSCODE_CHANNELS),
            "-c:a", "aac",
            "-b:a", "64k",
            "-movflags", "+faststart",
            output_path,
        ]
        logger.info("Transcode command (2-pass loudnorm linear=true)")
        apply = run_ffmpeg(apply_cmd, timeout=300)
        if apply.returncode != 0:
            logger.warning("Loudnorm pass2 failed, fallback to stable mode. err=%s", apply.stderr[-300:])
            raise RuntimeError("pass2 failed")
        return True, True

    except subprocess.TimeoutExpired:
        logger.error("FFmpeg timeout")
        return False, False
    except Exception:
        # Fallback: never run single-pass loudnorm. Apply only stable filters + limiter.
        stable_filter = post_chain()
        fallback_cmd = [
            *ffmpeg_base_input(),
            "-af", stable_filter,
            "-ar", str(TRANSCODE_SAMPLE_RATE),
            "-ac", str(TRANSCODE_CHANNELS),
            "-c:a", "aac",
            "-b:a", "64k",
            "-movflags", "+faststart",
            output_path,
        ]
        logger.info("Transcode fallback command (stable no-dynamic-loudnorm)")
        result = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error("FFmpeg fallback error: %s", result.stderr[-500:])
            return False, False
        return True, False


def process_transcode(message: dict) -> bool:
    """Process a transcode job."""
    file_id = message["file_id"]
    stored_filename = message["stored_filename"]

    logger.info("Transcoding file: %s (id=%s)", stored_filename, file_id)

    SessionLocal = create_session_factory(db_cfg)
    db = SessionLocal()

    try:
        file_obj = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
        if not file_obj:
            logger.error("File not found: %s", file_id)
            return True

        session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()

        # Update status
        file_obj.status = UploadStatus.TRANSCODING
        file_obj.status_message = "Transcodage et normalisation audio en cours..."
        db.commit()
        notify_portal(session_obj, file_obj, file_obj.status_message)

        # Download from upload-staging S3
        file_data = download_fileobj(s3_upload_cfg, stored_filename)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write input file
            input_ext = Path(stored_filename).suffix or ".audio"
            input_path = os.path.join(tmpdir, f"input{input_ext}")
            with open(input_path, "wb") as f:
                f.write(file_data.read())

            # Analyze quality before transcoding
            quality = analyze_audio_quality(input_path)

            # Transcode
            output_name = Path(stored_filename).stem + ".mp4"
            output_path = os.path.join(tmpdir, output_name)

            success, was_normalized = transcode_audio(
                input_path, output_path, file_duration_s=quality.get("duration", 0.0),
            )

            if not success:
                file_obj.status = UploadStatus.TRANSCODE_FAILED
                file_obj.status_message = "Échec du transcodage audio."
                db.commit()
                notify_portal(session_obj, file_obj, file_obj.status_message)
                return True

            # Upload transcoded file to processed-staging S3
            with open(output_path, "rb") as f:
                upload_fileobj(s3_processed_cfg, output_name, BytesIO(f.read()), "audio/mp4")

            # Mesure loudnorm source + output AVANT de quitter le tmpdir.
            # On persiste les valeurs en DB ; le bouton (i) côté mydevices
            # lit la DB plutôt que de redownloader la source qui sera
            # purgée plus tard. Best-effort : si une mesure échoue, on
            # laisse la colonne NULL (le frontend sait gérer).
            source_loud = _measure_loudnorm_quiet(input_path)
            output_loud = _measure_loudnorm_quiet(output_path)

            # Update DB
            file_obj.status = UploadStatus.TRANSCODED
            file_obj.transcoded_filename = output_name
            file_obj.audio_quality_score = quality["quality_score"]
            file_obj.audio_duration_seconds = quality["duration"]
            file_obj.audio_sample_rate = quality["sample_rate"]
            if source_loud:
                file_obj.normalization_source_i = source_loud.get("i")
                file_obj.normalization_source_tp = source_loud.get("tp")
                file_obj.normalization_source_lra = source_loud.get("lra")
            if output_loud:
                file_obj.normalization_output_i = output_loud.get("i")
                file_obj.normalization_output_tp = output_loud.get("tp")
                file_obj.normalization_output_lra = output_loud.get("lra")
            normalize_label = "normalisé" if was_normalized else "déjà au niveau, normalisation omise"
            file_obj.status_message = (
                f"Traitement terminé. Qualité audio : {quality['quality_score']}/5 "
                f"({quality['duration']:.1f}s, {normalize_label})"
            )
            db.commit()
            notify_portal(session_obj, file_obj, file_obj.status_message)

            # Publish to file-ready queue
            publish_message(rabbit_cfg, QUEUE_FILE_READY, {
                **message,
                "transcoded_filename": output_name,
                "quality_score": quality["quality_score"],
                "duration_seconds": quality["duration"],
                "was_normalized": was_normalized,
            })

            logger.info(
                "Transcoded %s → %s (quality=%.1f, duration=%.1fs, was_normalized=%s)",
                stored_filename, output_name,
                quality["quality_score"], quality["duration"], was_normalized,
            )
            return True

    except Exception:
        logger.exception("Error transcoding %s", file_id)
        return False
    finally:
        db.close()


def main():
    logger.info("Starting Transcode Worker...")
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    init_tables(db_cfg, ExternalBase)
    ensure_bucket(s3_processed_cfg)
    declare_queues(rabbit_cfg)
    consume_queue(rabbit_cfg, QUEUE_TRANSCODE, process_transcode)


if __name__ == "__main__":
    main()
