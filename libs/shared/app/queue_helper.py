"""RabbitMQ helper for publishing and consuming messages."""

import json
import logging
import os
import time
from typing import Callable, Optional, Tuple

import pika

from .config import RabbitMQConfig

logger = logging.getLogger(__name__)

# Queue names
QUEUE_AV_SCAN = "av_scan"
QUEUE_TRANSCODE = "transcode"
QUEUE_FILE_READY = "file_ready"
QUEUE_TRANSCRIPTION = "transcription"
QUEUE_INTERNAL_PULL = "internal_pull"
QUEUE_MCR_IMPORT = "mcr_import"

RETRY_HEADER = "x-retry-count"
DEFAULT_MAX_RETRIES = max(0, int(os.getenv("QUEUE_MAX_RETRIES", "5")))


def next_retry_decision(headers: Optional[dict], max_retries: int) -> Tuple[bool, dict, int]:
    """
    Decide whether a failed message should be retried.

    Returns (should_retry, new_headers_for_republish, current_retry_count).
    Pure function so it can be unit-tested without pika.
    """
    current = dict(headers or {})
    try:
        retry_count = int(current.get(RETRY_HEADER, 0))
    except (TypeError, ValueError):
        retry_count = 0
    if retry_count >= max_retries:
        return False, current, retry_count
    next_headers = dict(current)
    next_headers[RETRY_HEADER] = retry_count + 1
    return True, next_headers, retry_count + 1


def get_connection(cfg: RabbitMQConfig) -> pika.BlockingConnection:
    """
    Create a blocking connection to RabbitMQ with retry/backoff.
    Env:
      RABBITMQ_CONNECT_RETRY_DELAY_SECONDS (default: 3)
      RABBITMQ_CONNECT_MAX_RETRIES (default: 0 => infinite)
    """
    credentials = pika.PlainCredentials(cfg.user, cfg.password)
    params = pika.ConnectionParameters(
        host=cfg.host,
        port=cfg.port,
        virtual_host=cfg.vhost,
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )
    retry_delay = max(1, int(os.getenv("RABBITMQ_CONNECT_RETRY_DELAY_SECONDS", "3")))
    max_retries = max(0, int(os.getenv("RABBITMQ_CONNECT_MAX_RETRIES", "0")))

    attempt = 0
    while True:
        try:
            return pika.BlockingConnection(params)
        except Exception:
            attempt += 1
            if max_retries and attempt >= max_retries:
                logger.exception(
                    "RabbitMQ connection failed after %s attempt(s): %s:%s vhost=%s",
                    attempt, cfg.host, cfg.port, cfg.vhost
                )
                raise
            logger.warning(
                "RabbitMQ unavailable (attempt %s): %s:%s vhost=%s; retrying in %ss",
                attempt, cfg.host, cfg.port, cfg.vhost, retry_delay
            )
            time.sleep(retry_delay)


def declare_queues(cfg: RabbitMQConfig):
    """Declare all queues with durability."""
    conn = get_connection(cfg)
    channel = conn.channel()
    for queue_name in [
        QUEUE_AV_SCAN,
        QUEUE_TRANSCODE,
        QUEUE_FILE_READY,
        QUEUE_TRANSCRIPTION,
        QUEUE_INTERNAL_PULL,
        QUEUE_MCR_IMPORT,
    ]:
        channel.queue_declare(queue=queue_name, durable=True)
        logger.info("Declared queue: %s", queue_name)
    conn.close()


def publish_message(cfg: RabbitMQConfig, queue: str, message: dict):
    """Publish a JSON message to a queue."""
    conn = get_connection(cfg)
    channel = conn.channel()
    channel.queue_declare(queue=queue, durable=True)
    channel.basic_publish(
        exchange="",
        routing_key=queue,
        body=json.dumps(message),
        properties=pika.BasicProperties(
            delivery_mode=2,  # persistent
            content_type="application/json",
        ),
    )
    logger.info("Published to %s: file_id=%s", queue, message.get("file_id", "?"))
    conn.close()


def consume_queue(
    cfg: RabbitMQConfig,
    queue: str,
    callback: Callable[[dict], bool],
    prefetch: int = 1,
    max_retries: Optional[int] = None,
    on_give_up: Optional[Callable[[dict, int], None]] = None,
):
    """
    Consume messages from a queue. callback receives the parsed message dict.

    Retry policy: instead of basic_nack(requeue=True) on failure (which loops
    forever and lets a single poisoned message monopolise a worker), each
    failure republishes the message with an incremented x-retry-count header
    and acks the original delivery. After max_retries attempts the message is
    dropped (acked) and on_give_up is invoked if provided so the application
    can mark the underlying record as failed.

    max_retries defaults to QUEUE_MAX_RETRIES env var (5).
    """
    if max_retries is None:
        max_retries = DEFAULT_MAX_RETRIES

    conn = get_connection(cfg)
    channel = conn.channel()
    channel.queue_declare(queue=queue, durable=True)
    channel.basic_qos(prefetch_count=prefetch)

    def _republish_or_drop(ch, method, properties, body, message):
        headers = (properties.headers if properties else None)
        should_retry, new_headers, count = next_retry_decision(headers, max_retries)
        if should_retry:
            ch.basic_publish(
                exchange="",
                routing_key=queue,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type=(properties.content_type if properties else "application/json"),
                    headers=new_headers,
                ),
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)
            logger.warning(
                "Republished to %s for retry %d/%d: file_id=%s",
                queue, count, max_retries, message.get("file_id", "?"),
            )
            return
        # Out of retries: drop and notify the application so it can record failure.
        ch.basic_ack(delivery_tag=method.delivery_tag)
        logger.error(
            "Dropping message from %s after %d retries: file_id=%s",
            queue, count, message.get("file_id", "?"),
        )
        if on_give_up is not None:
            try:
                on_give_up(message, count)
            except Exception:
                logger.exception("on_give_up callback raised for %s", queue)

    def _on_message(ch, method, properties, body):
        try:
            message = json.loads(body)
        except Exception:
            # Unparseable payload: drop immediately, no retry value.
            logger.exception("Unparseable message on %s, dropping", queue)
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return
        logger.info("Consuming from %s: %s", queue, message.get("file_id", "?"))
        try:
            success = callback(message)
        except Exception:
            logger.exception("Error processing message from %s", queue)
            success = False
        if success:
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return
        _republish_or_drop(ch, method, properties, body, message)

    channel.basic_consume(queue=queue, on_message_callback=_on_message)
    logger.info("Waiting for messages on %s...", queue)
    channel.start_consuming()


def drain_queue_once(
    cfg: RabbitMQConfig,
    queue: str,
    callback: Callable[[dict], bool],
    max_retries: Optional[int] = None,
    on_give_up: Optional[Callable[[dict, int], None]] = None,
) -> int:
    """
    Drain a queue with basic.get until empty, then close. Returns the number
    of messages handled (acked + republished combined).

    Used for periodic polling on the consumer side: a worker can call this
    every N seconds in a thread instead of running a long-lived basic.consume.
    The same retry-counter / republish / give-up policy as consume_queue
    applies — keep the two paths semantically identical so messages behave
    the same whether they were delivered via push subscription or pull poll.
    """
    if max_retries is None:
        max_retries = DEFAULT_MAX_RETRIES

    conn = get_connection(cfg)
    channel = conn.channel()
    channel.queue_declare(queue=queue, durable=True)

    handled = 0
    try:
        while True:
            method, properties, body = channel.basic_get(queue=queue, auto_ack=False)
            if method is None:
                break
            handled += 1
            try:
                message = json.loads(body)
            except Exception:
                logger.exception("Unparseable message on %s, dropping", queue)
                channel.basic_ack(delivery_tag=method.delivery_tag)
                continue
            logger.info("Drained from %s: %s", queue, message.get("file_id", "?"))
            try:
                success = callback(message)
            except Exception:
                logger.exception("Error processing drained message from %s", queue)
                success = False
            if success:
                channel.basic_ack(delivery_tag=method.delivery_tag)
                continue
            headers = (properties.headers if properties else None)
            should_retry, new_headers, count = next_retry_decision(headers, max_retries)
            if should_retry:
                channel.basic_publish(
                    exchange="",
                    routing_key=queue,
                    body=body,
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        content_type=(properties.content_type if properties else "application/json"),
                        headers=new_headers,
                    ),
                )
                channel.basic_ack(delivery_tag=method.delivery_tag)
                logger.warning(
                    "Republished to %s for retry %d/%d: file_id=%s",
                    queue, count, max_retries, message.get("file_id", "?"),
                )
                continue
            channel.basic_ack(delivery_tag=method.delivery_tag)
            logger.error(
                "Dropping message from %s after %d retries: file_id=%s",
                queue, count, message.get("file_id", "?"),
            )
            if on_give_up is not None:
                try:
                    on_give_up(message, count)
                except Exception:
                    logger.exception("on_give_up callback raised for %s", queue)
    finally:
        try:
            conn.close()
        except Exception:
            logger.warning("Failed to close drain connection cleanly", exc_info=True)
    return handled
