"""RabbitMQ helper for publishing and consuming messages."""

import json
import logging
from typing import Callable, Optional

import pika

from .config import RabbitMQConfig

logger = logging.getLogger(__name__)

# Queue names
QUEUE_AV_SCAN = "av_scan"
QUEUE_TRANSCODE = "transcode"
QUEUE_FILE_READY = "file_ready"
QUEUE_TRANSCRIPTION = "transcription"


def get_connection(cfg: RabbitMQConfig) -> pika.BlockingConnection:
    """Create a blocking connection to RabbitMQ."""
    credentials = pika.PlainCredentials(cfg.user, cfg.password)
    params = pika.ConnectionParameters(
        host=cfg.host,
        port=cfg.port,
        virtual_host=cfg.vhost,
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )
    return pika.BlockingConnection(params)


def declare_queues(cfg: RabbitMQConfig):
    """Declare all queues with durability."""
    conn = get_connection(cfg)
    channel = conn.channel()
    for queue_name in [QUEUE_AV_SCAN, QUEUE_TRANSCODE, QUEUE_FILE_READY, QUEUE_TRANSCRIPTION]:
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


def consume_queue(cfg: RabbitMQConfig, queue: str, callback: Callable[[dict], bool], prefetch: int = 1):
    """
    Consume messages from a queue. callback receives the parsed message dict.
    If callback returns True, message is acked. If False or exception, nacked with requeue.
    """
    conn = get_connection(cfg)
    channel = conn.channel()
    channel.queue_declare(queue=queue, durable=True)
    channel.basic_qos(prefetch_count=prefetch)

    def _on_message(ch, method, properties, body):
        try:
            message = json.loads(body)
            logger.info("Consuming from %s: %s", queue, message.get("file_id", "?"))
            success = callback(message)
            if success:
                ch.basic_ack(delivery_tag=method.delivery_tag)
            else:
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        except Exception:
            logger.exception("Error processing message from %s", queue)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    channel.basic_consume(queue=queue, on_message_callback=_on_message)
    logger.info("Waiting for messages on %s...", queue)
    channel.start_consuming()
