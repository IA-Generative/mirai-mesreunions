"""Unit tests for KeventClient.transcribe_async(initial_prompt=…) (§5.1bis).

L'objectif est de vérifier que :
  - le kwarg ``initial_prompt`` est bien propagé via ``extra_form`` dans
    submit_job
  - sans ``initial_prompt`` il n'apparaît pas dans le payload (pas de
    régression)
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _load_kevent_client():
    """Importe kevent_client.py en standalone (sans tirer le reste de
    file-mover/app/__init__).
    """
    path = os.path.join(ROOT, "services", "file-mover", "app", "kevent_client.py")
    spec = importlib.util.spec_from_file_location("kc_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kc = _load_kevent_client()


def _make_client():
    """Instancie un KeventClient minimal avec submit_job + wait_for_job
    monkey-patched pour capturer les arguments."""
    client = kc.KeventClient(
        gateway_url="http://kevent.example",
        api_key="K" * 40,
        transcription_model="whisper-1",
    )
    client.submit_job = MagicMock(return_value="job-id-42")
    client.wait_for_job = MagicMock(return_value={"text": "hello world"})
    return client


def test_transcribe_async_passes_initial_prompt_in_extra_form():
    client = _make_client()
    client.transcribe_async(
        b"binary-audio", "f.m4a", "audio/mp4",
        initial_prompt="Réunion entre Jean Dupont et Marie sur DTNUM.",
    )
    args, kwargs = client.submit_job.call_args
    extra = kwargs.get("extra_form") or {}
    assert "initial_prompt" in extra
    assert "DTNUM" in extra["initial_prompt"]


def test_transcribe_async_without_initial_prompt_does_not_add_field():
    client = _make_client()
    client.transcribe_async(b"audio", "f.m4a", "audio/mp4")
    args, kwargs = client.submit_job.call_args
    extra = kwargs.get("extra_form") or {}
    assert "initial_prompt" not in extra


def test_transcribe_async_empty_initial_prompt_is_dropped():
    client = _make_client()
    client.transcribe_async(b"audio", "f.m4a", "audio/mp4", initial_prompt="")
    args, kwargs = client.submit_job.call_args
    extra = kwargs.get("extra_form") or {}
    assert "initial_prompt" not in extra
