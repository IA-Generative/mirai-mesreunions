"""Tests pour libs.shared.app.queue_eta.compute_queue_summary.

Couvre les 5 cas demandés par la spec :
  a) listing vide
  b) own_job_id en position 1 → eta = 10s (arrondi)
  c) own_job_id en position 5 → eta = 40s
  d) own_job_id absent (déjà picked up) → your_position=None, eta_seconds=None
  e) stale=True si tous les updated_at > 30s
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "libs"))

from shared.app.queue_eta import compute_queue_summary, QueueSummary  # noqa: E402


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


def _job(job_id, status, position=None, updated_offset_s=-2):
    """Helper : construit un job dict comme renvoyé par la gateway."""
    j = {
        "job_id": job_id,
        "service_type": "audio",
        "model": "faster-whisper-large-v3-turbo",
        "status": status,
        "created_at": (_NOW + timedelta(seconds=updated_offset_s - 5)).isoformat(),
        "updated_at": (_NOW + timedelta(seconds=updated_offset_s)).isoformat(),
    }
    if position is not None:
        j["queue_position"] = position
    return j


# ─────────────────────────────────────────────────────────────────────────
# (a) listing vide → tous champs neutres
# ─────────────────────────────────────────────────────────────────────────
def test_empty_listing():
    summary = compute_queue_summary({"jobs": [], "total": 0}, own_job_id="abc", now=_NOW)
    assert summary.pending_total == 0
    assert summary.processing_total == 0
    assert summary.your_position is None
    assert summary.eta_seconds is None
    assert summary.stale is False  # liste vide ≠ stale, juste pas de jobs
    assert summary.throughput_per_min is not None  # avg_s > 0 → calculé
    assert summary.fetched_at.endswith("+00:00")


# ─────────────────────────────────────────────────────────────────────────
# (b) own_job_id en position 1 → eta = 10s (arrondi 10s)
# ─────────────────────────────────────────────────────────────────────────
def test_own_job_first_position():
    payload = {
        "jobs": [
            _job("my-job", "pending", position=1),
            _job("other-1", "pending", position=2),
            _job("other-2", "pending", position=3),
        ],
        "total": 3,
    }
    s = compute_queue_summary(payload, own_job_id="my-job", now=_NOW, avg_process_s=8.0)
    assert s.your_position == 1
    # 1 * 8.0 = 8 → arrondi 10s = 10
    assert s.eta_seconds == 10
    assert s.pending_total == 3
    assert s.processing_total == 0


# ─────────────────────────────────────────────────────────────────────────
# (c) own_job_id en position 5 → eta = 40s
# ─────────────────────────────────────────────────────────────────────────
def test_own_job_position_5():
    payload = {
        "jobs": [_job(f"job-{i}", "pending", position=i) for i in range(1, 8)],
        "total": 7,
    }
    s = compute_queue_summary(payload, own_job_id="job-5", now=_NOW, avg_process_s=8.0)
    assert s.your_position == 5
    # 5 * 8.0 = 40 → arrondi 10s = 40
    assert s.eta_seconds == 40
    assert s.pending_total == 7


# ─────────────────────────────────────────────────────────────────────────
# (d) own_job_id absent (déjà picked) → your_position=None, eta=None
# ─────────────────────────────────────────────────────────────────────────
def test_own_job_already_picked():
    payload = {
        "jobs": [
            _job("other-1", "pending", position=1),
            _job("other-2", "processing"),
        ],
        "total": 2,
    }
    s = compute_queue_summary(payload, own_job_id="my-removed-job", now=_NOW)
    assert s.your_position is None
    assert s.eta_seconds is None
    assert s.pending_total == 1  # autres jobs présents
    assert s.processing_total == 1


# ─────────────────────────────────────────────────────────────────────────
# (e) stale=True si tous les updated_at > 30s avant now
# ─────────────────────────────────────────────────────────────────────────
def test_stale_when_all_updates_are_old():
    payload = {
        "jobs": [
            _job("a", "pending", position=1, updated_offset_s=-120),
            _job("b", "pending", position=2, updated_offset_s=-90),
        ],
        "total": 2,
    }
    s = compute_queue_summary(payload, own_job_id=None, now=_NOW)
    assert s.stale is True


def test_not_stale_when_recent_update():
    payload = {
        "jobs": [
            _job("a", "pending", position=1, updated_offset_s=-5),   # récent
            _job("b", "pending", position=2, updated_offset_s=-120),  # vieux
        ],
        "total": 2,
    }
    s = compute_queue_summary(payload, own_job_id=None, now=_NOW)
    # max(updated_at) = -5s → pas stale
    assert s.stale is False
