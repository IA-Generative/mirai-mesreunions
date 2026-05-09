"""
Unit tests for the internal_pull queue helpers (constant declaration plus
``drain_queue_once`` periodic-poll function added in this PR).

We stub pika so importing queue_helper does not require RabbitMQ. Same
pattern as ``test_queue_helper_retry.py``.
"""

import importlib.util
import json
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# --- pika stub (must be installed before importing queue_helper) ----------

class _Method:
    def __init__(self, delivery_tag):
        self.delivery_tag = delivery_tag


class _Properties:
    def __init__(self, headers=None, content_type="application/json", delivery_mode=2):
        self.headers = dict(headers) if headers else None
        self.content_type = content_type
        self.delivery_mode = delivery_mode


class _FakeChannel:
    """In-memory queue. basic_get pops from the front, basic_publish pushes."""

    def __init__(self):
        self._queue = []  # list of (body_bytes, properties)
        self.acks = []
        self.declared = []
        self._next_tag = 1

    def queue_declare(self, queue, durable=True):
        self.declared.append((queue, durable))

    def basic_get(self, queue, auto_ack=False):
        if not self._queue:
            return (None, None, None)
        body, props = self._queue.pop(0)
        method = _Method(self._next_tag)
        self._next_tag += 1
        return method, props, body

    def basic_publish(self, exchange, routing_key, body, properties=None):
        # Treat a publish to the same routing_key as an enqueue (durable
        # republish path used by drain_queue_once for retries).
        self._queue.append((body, properties))

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)

    def basic_qos(self, prefetch_count):
        pass

    def close(self):
        pass


class _FakeConnection:
    def __init__(self):
        self.channel_inst = _FakeChannel()

    def channel(self):
        return self.channel_inst

    def close(self):
        pass


pika_stub = types.ModuleType("pika")
pika_stub.BasicProperties = _Properties
pika_stub.PlainCredentials = lambda *a, **kw: None
pika_stub.ConnectionParameters = lambda *a, **kw: None
_LAST_CONN = {"conn": None}
def _make_conn(*_a, **_kw):
    _LAST_CONN["conn"] = _FakeConnection()
    return _LAST_CONN["conn"]
pika_stub.BlockingConnection = _make_conn
sys.modules["pika"] = pika_stub

# Stub the relative .config import
config_stub = types.ModuleType("libs.shared.app.config")
class _RMQ:
    host = "x"; port = 5672; user = "u"; password = "p"; vhost = "/"
    @property
    def url(self): return ""
config_stub.RabbitMQConfig = _RMQ
sys.modules.setdefault("libs", types.ModuleType("libs"))
sys.modules.setdefault("libs.shared", types.ModuleType("libs.shared"))
sys.modules.setdefault("libs.shared.app", types.ModuleType("libs.shared.app"))
sys.modules["libs.shared.app.config"] = config_stub

MODULE_PATH = os.path.join(ROOT, "libs", "shared", "app", "queue_helper.py")
SPEC = importlib.util.spec_from_file_location("queue_helper_under_test_pull", MODULE_PATH)
QH = importlib.util.module_from_spec(SPEC)
QH.__package__ = "libs.shared.app"
sys.modules["libs.shared.app.queue_helper_under_test_pull"] = QH
SPEC.loader.exec_module(QH)


# --- Tests ----------------------------------------------------------------

def test_internal_pull_constant_exposed():
    assert QH.QUEUE_INTERNAL_PULL == "internal_pull"


def test_declare_queues_includes_internal_pull():
    cfg = _RMQ()
    QH.declare_queues(cfg)
    declared_names = [name for name, _ in _LAST_CONN["conn"].channel_inst.declared]
    assert "internal_pull" in declared_names
    # Ensure the original four are still there.
    for q in ("av_scan", "transcode", "file_ready", "transcription"):
        assert q in declared_names


def _enqueue(channel, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    channel._queue.append((body, _Properties(headers=headers)))


def test_drain_empty_queue_returns_zero():
    cfg = _RMQ()
    handled = QH.drain_queue_once(cfg, "internal_pull", lambda m: True)
    assert handled == 0


def test_drain_single_message_success_acks_once():
    cfg = _RMQ()
    fixed_conn = _FakeConnection()
    sys.modules["pika"].BlockingConnection = lambda *a, **kw: fixed_conn
    try:
        ch = fixed_conn.channel_inst
        _enqueue(ch, {"file_id": "abc"})
        seen = []

        def cb(msg):
            seen.append(msg)
            return True

        handled = QH.drain_queue_once(cfg, "internal_pull", cb)
        assert handled == 1
        assert seen == [{"file_id": "abc"}]
        assert ch.acks == [1]
        assert ch._queue == []
    finally:
        sys.modules["pika"].BlockingConnection = _make_conn


def test_drain_callback_failure_republishes_with_retry_counter():
    """First failure on a fresh message: ack + republish with x-retry-count=1.

    drain_queue_once loops until the queue is empty, so to observe just the
    first republish we use max_retries=1 and a callback that succeeds on the
    second attempt — i.e. we observe one republish, then one successful pull.
    """
    cfg = _RMQ()
    fixed_conn = _FakeConnection()
    sys.modules["pika"].BlockingConnection = lambda *a, **kw: fixed_conn
    try:
        ch = fixed_conn.channel_inst
        _enqueue(ch, {"file_id": "abc"})
        attempts = []

        def cb(msg):
            attempts.append(msg)
            return len(attempts) > 1  # fail first, succeed second

        QH.drain_queue_once(cfg, "internal_pull", cb, max_retries=5)
        # Two callback invocations: first failed (republished), second succeeded.
        assert len(attempts) == 2
        # Two acks total: one for the original delivery (after republish), one
        # for the republished delivery (success).
        assert len(ch.acks) == 2
        # Final state: queue empty.
        assert ch._queue == []
    finally:
        sys.modules["pika"].BlockingConnection = _make_conn


def test_drain_first_failure_carries_x_retry_count_one_on_republish():
    """Snapshot the republished body's headers before the loop drains it."""
    cfg = _RMQ()
    fixed_conn = _FakeConnection()
    # Capture the *intermediate* republish before the next iteration consumes it.
    seen_publishes = []
    original_publish = fixed_conn.channel_inst.basic_publish

    def spy_publish(exchange, routing_key, body, properties=None):
        seen_publishes.append((body, properties))
        return original_publish(exchange, routing_key, body, properties=properties)

    fixed_conn.channel_inst.basic_publish = spy_publish
    sys.modules["pika"].BlockingConnection = lambda *a, **kw: fixed_conn
    try:
        ch = fixed_conn.channel_inst
        _enqueue(ch, {"file_id": "abc"})
        QH.drain_queue_once(cfg, "internal_pull", lambda m: False, max_retries=2)
        # Two republishes (retry=1, retry=2), then drop on third failure.
        assert len(seen_publishes) == 2
        first_body, first_props = seen_publishes[0]
        assert json.loads(first_body)["file_id"] == "abc"
        assert first_props.headers.get("x-retry-count") == 1
        _, second_props = seen_publishes[1]
        assert second_props.headers.get("x-retry-count") == 2
        # Queue empty at the end.
        assert ch._queue == []
    finally:
        sys.modules["pika"].BlockingConnection = _make_conn


def test_drain_dropped_after_max_retries_no_republish():
    cfg = _RMQ()
    fixed_conn = _FakeConnection()
    sys.modules["pika"].BlockingConnection = lambda *a, **kw: fixed_conn
    try:
        ch = fixed_conn.channel_inst
        _enqueue(ch, {"file_id": "abc"}, headers={"x-retry-count": 5})
        gave_up = []
        QH.drain_queue_once(
            cfg, "internal_pull",
            lambda m: False,
            max_retries=5,
            on_give_up=lambda msg, count: gave_up.append((msg, count)),
        )
        # Acked but not republished.
        assert ch.acks == [1]
        assert ch._queue == []
        # Give-up hook fired.
        assert gave_up == [({"file_id": "abc"}, 5)]
    finally:
        sys.modules["pika"].BlockingConnection = _make_conn


def test_drain_unparseable_payload_dropped_silently():
    cfg = _RMQ()
    fixed_conn = _FakeConnection()
    sys.modules["pika"].BlockingConnection = lambda *a, **kw: fixed_conn
    try:
        ch = fixed_conn.channel_inst
        ch._queue.append((b"not json at all", _Properties()))
        gave_up = []
        QH.drain_queue_once(
            cfg, "internal_pull",
            lambda m: True,  # never invoked, payload is junk
            on_give_up=lambda msg, count: gave_up.append((msg, count)),
        )
        assert ch.acks == [1]   # message acked (dropped)
        assert ch._queue == []  # not republished
        assert gave_up == []    # not a "give-up"; we drop bad payloads silently
    finally:
        sys.modules["pika"].BlockingConnection = _make_conn


def test_drain_callback_exception_treated_as_failure():
    """An exception in the callback must be caught and trigger the retry path."""
    cfg = _RMQ()
    fixed_conn = _FakeConnection()
    sys.modules["pika"].BlockingConnection = lambda *a, **kw: fixed_conn
    try:
        ch = fixed_conn.channel_inst
        _enqueue(ch, {"file_id": "abc"})

        attempts = []

        def maybe_boom(msg):
            attempts.append(msg)
            if len(attempts) == 1:
                raise RuntimeError("simulated S3 outage")
            return True

        QH.drain_queue_once(cfg, "internal_pull", maybe_boom, max_retries=5)
        # Drain processed at least the original (raised) and the republished
        # (succeeded) — proving the exception path mapped to a retry, not a crash.
        assert len(attempts) >= 2
        assert ch._queue == []
    finally:
        sys.modules["pika"].BlockingConnection = _make_conn


def test_drain_continues_until_queue_empty():
    cfg = _RMQ()
    fixed_conn = _FakeConnection()
    sys.modules["pika"].BlockingConnection = lambda *a, **kw: fixed_conn
    try:
        ch = fixed_conn.channel_inst
        for fid in ("a", "b", "c", "d"):
            _enqueue(ch, {"file_id": fid})
        seen = []
        handled = QH.drain_queue_once(
            cfg, "internal_pull",
            lambda m: (seen.append(m["file_id"]), True)[1],
        )
        assert handled == 4
        assert seen == ["a", "b", "c", "d"]
        assert ch._queue == []
    finally:
        sys.modules["pika"].BlockingConnection = _make_conn
