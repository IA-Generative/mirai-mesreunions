"""
Tests for the retry decision in queue_helper.

Covers the pure function `next_retry_decision`, which decides whether a failed
message should be republished with an incremented retry counter or dropped.

We import the module via importlib so the test does not require a real
RabbitMQ connection. Pika is imported at module load by queue_helper, so we
keep the import surface narrow by patching it out before exec.
"""

import importlib.util
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Stub pika so importing queue_helper doesn't require the real package.
if "pika" not in sys.modules:
    pika_stub = types.ModuleType("pika")
    pika_stub.BasicProperties = lambda **kwargs: types.SimpleNamespace(**kwargs)
    pika_stub.PlainCredentials = lambda *a, **kw: None
    pika_stub.ConnectionParameters = lambda *a, **kw: None
    pika_stub.BlockingConnection = lambda *a, **kw: None
    sys.modules["pika"] = pika_stub

# Stub the relative .config import path used by queue_helper.
config_stub = types.ModuleType("libs.shared.app.config")
class _RMQ:  # noqa: N801 — match dataclass naming used in real module
    pass
config_stub.RabbitMQConfig = _RMQ
sys.modules.setdefault("libs", types.ModuleType("libs"))
sys.modules.setdefault("libs.shared", types.ModuleType("libs.shared"))
sys.modules.setdefault("libs.shared.app", types.ModuleType("libs.shared.app"))
sys.modules["libs.shared.app.config"] = config_stub

MODULE_PATH = os.path.join(ROOT, "libs", "shared", "app", "queue_helper.py")
SPEC = importlib.util.spec_from_file_location("queue_helper_under_test", MODULE_PATH)
QH = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
# queue_helper.py uses `from .config import RabbitMQConfig`. Set its package
# context so the relative import resolves to our stub.
QH.__package__ = "libs.shared.app"
sys.modules["libs.shared.app.queue_helper_under_test"] = QH
SPEC.loader.exec_module(QH)

next_retry_decision = QH.next_retry_decision
RETRY_HEADER = QH.RETRY_HEADER


def test_first_failure_should_retry_with_count_one():
    should_retry, headers, count = next_retry_decision(None, max_retries=5)
    assert should_retry is True
    assert count == 1
    assert headers[RETRY_HEADER] == 1


def test_existing_count_increments():
    should_retry, headers, count = next_retry_decision({RETRY_HEADER: 3}, max_retries=5)
    assert should_retry is True
    assert count == 4
    assert headers[RETRY_HEADER] == 4


def test_at_max_should_not_retry():
    should_retry, _, count = next_retry_decision({RETRY_HEADER: 5}, max_retries=5)
    assert should_retry is False
    assert count == 5


def test_above_max_should_not_retry():
    """Defensive: if a stale message somehow exceeds max, do not loop forever."""
    should_retry, _, count = next_retry_decision({RETRY_HEADER: 99}, max_retries=5)
    assert should_retry is False
    assert count == 99


def test_zero_max_drops_immediately():
    """max_retries=0 means no retry — first failure is terminal."""
    should_retry, _, _ = next_retry_decision(None, max_retries=0)
    assert should_retry is False


def test_other_headers_are_preserved():
    """User-defined headers (e.g. trace IDs) must survive the republish."""
    initial = {"trace-id": "abc-123", "x-priority": "high"}
    _, headers, _ = next_retry_decision(initial, max_retries=5)
    assert headers["trace-id"] == "abc-123"
    assert headers["x-priority"] == "high"


def test_garbage_retry_header_treated_as_zero():
    """A corrupted retry counter must not crash the consumer."""
    should_retry, headers, count = next_retry_decision(
        {RETRY_HEADER: "not-an-int"}, max_retries=5
    )
    assert should_retry is True
    assert count == 1
    assert headers[RETRY_HEADER] == 1


def test_input_headers_not_mutated():
    """Caller's dict must not be modified — we return a new dict."""
    initial = {RETRY_HEADER: 2, "trace-id": "t"}
    snapshot = dict(initial)
    next_retry_decision(initial, max_retries=5)
    assert initial == snapshot
