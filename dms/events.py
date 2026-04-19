"""
Event publisher — publishes DMS API events to Redpanda (Kafka-compatible).

Every significant action in the API emits a structured event to a topic.
The stream consumer (services/stream_consumer/) reads these topics and
computes rolling window features stored in Redis sorted sets.

Topics:
  dms.uploads    → new upload initiated
  dms.approvals  → upload approved or rejected
  dms.inference  → inference request made (image → Qdrant query)
  dms.jobs       → job status changes (queued / running / succeeded / failed)

Event shape (all topics):
  {
    "event_type": "<topic suffix>",
    "timestamp":  "<ISO8601 UTC>",
    "payload":    { ... event-specific fields ... }
  }

Publishing is fire-and-forget: if Redpanda is unavailable, the event is
dropped and a warning is logged. The API never fails because of a missing
event — application state (Postgres) is always the source of truth.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Topic names
TOPIC_UPLOADS   = "dms.uploads"
TOPIC_APPROVALS = "dms.approvals"
TOPIC_INFERENCE = "dms.inference"
TOPIC_JOBS      = "dms.jobs"

_producer = None


def _get_producer():
    global _producer
    if _producer is not None:
        return _producer

    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
    try:
        from confluent_kafka import Producer
        _producer = Producer({"bootstrap.servers": bootstrap})
        log.info("Kafka producer connected to %s", bootstrap)
    except Exception as exc:
        log.warning("Kafka producer init failed (events disabled): %s", exc)
        _producer = None
    return _producer


def _publish(topic: str, payload: dict[str, Any]) -> None:
    """Fire-and-forget publish. Never raises — drops silently if Kafka is down."""
    producer = _get_producer()
    if producer is None:
        return

    event = {
        "event_type": topic.split(".")[-1],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    try:
        producer.produce(topic, value=json.dumps(event).encode("utf-8"))
        producer.poll(0)
    except Exception as exc:
        log.warning("Failed to publish event to %s: %s", topic, exc)


# ── Public helpers ────────────────────────────────────────────────────────────

def emit_upload_created(upload_id: int, user_id: str, filename: str) -> None:
    _publish(TOPIC_UPLOADS, {
        "upload_id": upload_id,
        "user_id":   user_id,
        "filename":  filename,
    })


def emit_upload_approved(upload_id: int, user_id: str, approved: bool, object_key: str | None = None) -> None:
    _publish(TOPIC_APPROVALS, {
        "upload_id":  upload_id,
        "user_id":    user_id,
        "approved":   approved,
        "object_key": object_key,
    })


def emit_inference_request(upload_id: int | None, top_k: int, match_count: int) -> None:
    _publish(TOPIC_INFERENCE, {
        "upload_id":   upload_id,
        "top_k":       top_k,
        "match_count": match_count,
    })


def emit_job_status(job_id: int, kind: str, status: str) -> None:
    _publish(TOPIC_JOBS, {
        "job_id": job_id,
        "kind":   kind,
        "status": status,
    })
