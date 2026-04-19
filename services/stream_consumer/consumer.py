"""
Stream consumer — reads DMS events from Redpanda and computes rolling
window features stored in Redis sorted sets.

Redis data model (mirrors the GourmetGram data platform pattern):

  upload:<upload_id>:views:5min    → sorted set, score=unix_ts, member=event_token
  upload:<upload_id>:views:1hr     → sorted set
  upload:<upload_id>:approvals     → sorted set
  user:<user_id>:uploads           → sorted set (all uploads by this user)
  user:<user_id>:rejections        → sorted set (rejected uploads)
  inference:requests:5min          → sorted set (system-wide inference rate)

Windows are maintained by trimming members older than the window width
using ZREMRANGEBYSCORE before each ZADD.

These features are consumed by:
  - risk_scorer.py     → per-upload risk scoring
  - Airflow DAGs       → building Iceberg training snapshots with time-anchored features
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

import redis

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

KAFKA_BOOTSTRAP  = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
KAFKA_GROUP      = os.getenv("KAFKA_CONSUMER_GROUP",    "dms-stream-consumer")
KAFKA_TOPICS     = os.getenv("KAFKA_TOPICS", "dms.uploads,dms.approvals,dms.inference").split(",")
REDIS_URL        = os.getenv("REDIS_URL", "redis://redis:6379/2")  # DB 2 — feature store

WINDOW_5MIN  = 5 * 60
WINDOW_1HR   = 60 * 60
KEY_TTL      = 2 * 60 * 60   # expire feature keys after 2 hours of inactivity


def _redis_client() -> redis.Redis:
    return redis.from_url(REDIS_URL, decode_responses=True)


def _trim_window(r: redis.Redis, key: str, window_seconds: int, now: float) -> None:
    r.zremrangebyscore(key, "-inf", now - window_seconds)


def _add_event(r: redis.Redis, key: str, window_seconds: int, now: float) -> None:
    member = str(uuid.uuid4())
    _trim_window(r, key, window_seconds, now)
    r.zadd(key, {member: now})
    r.expire(key, KEY_TTL)


def process_upload_event(r: redis.Redis, payload: dict) -> None:
    upload_id = payload.get("upload_id")
    user_id   = payload.get("user_id")
    now       = time.time()

    if upload_id:
        _add_event(r, f"upload:{upload_id}:created:5min", WINDOW_5MIN, now)
        _add_event(r, f"upload:{upload_id}:created:1hr",  WINDOW_1HR,  now)

    if user_id:
        _add_event(r, f"user:{user_id}:uploads:5min", WINDOW_5MIN, now)
        _add_event(r, f"user:{user_id}:uploads:1hr",  WINDOW_1HR,  now)
        r.incr(f"user:{user_id}:total_uploads")
        r.expire(f"user:{user_id}:total_uploads", KEY_TTL)

    log.debug("Processed upload event upload_id=%s user_id=%s", upload_id, user_id)


def process_approval_event(r: redis.Redis, payload: dict) -> None:
    upload_id = payload.get("upload_id")
    user_id   = payload.get("user_id")
    approved  = payload.get("approved", True)
    now       = time.time()

    if not approved and user_id:
        _add_event(r, f"user:{user_id}:rejections:5min", WINDOW_5MIN, now)
        _add_event(r, f"user:{user_id}:rejections:1hr",  WINDOW_1HR,  now)
        r.incr(f"user:{user_id}:total_rejections")
        r.expire(f"user:{user_id}:total_rejections", KEY_TTL)

    if upload_id:
        r.set(f"upload:{upload_id}:approved", "1" if approved else "0", ex=KEY_TTL)

    log.debug("Processed approval event upload_id=%s approved=%s", upload_id, approved)


def process_inference_event(r: redis.Redis, payload: dict) -> None:
    now = time.time()
    _add_event(r, "inference:requests:5min", WINDOW_5MIN, now)
    _add_event(r, "inference:requests:1hr",  WINDOW_1HR,  now)
    log.debug("Processed inference event top_k=%s", payload.get("top_k"))


HANDLERS = {
    "uploads":   process_upload_event,
    "approvals": process_approval_event,
    "inference": process_inference_event,
}


def run() -> None:
    log.info("Starting stream consumer. Topics: %s", KAFKA_TOPICS)
    r = _redis_client()

    try:
        from confluent_kafka import Consumer, KafkaError
    except ImportError:
        log.error("confluent-kafka not installed. Run: pip install confluent-kafka")
        return

    consumer = Consumer({
        "bootstrap.servers":  KAFKA_BOOTSTRAP,
        "group.id":           KAFKA_GROUP,
        "auto.offset.reset":  "earliest",
        "enable.auto.commit": True,
    })
    consumer.subscribe(KAFKA_TOPICS)
    log.info("Subscribed to Kafka topics: %s", KAFKA_TOPICS)

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    log.error("Kafka error: %s", msg.error())
                continue

            try:
                event = json.loads(msg.value().decode("utf-8"))
                event_type = event.get("event_type", "")
                payload    = event.get("payload", {})
                handler    = HANDLERS.get(event_type)
                if handler:
                    handler(r, payload)
                else:
                    log.debug("No handler for event_type=%s", event_type)
            except Exception as exc:
                log.warning("Failed to process message: %s", exc)
    finally:
        consumer.close()
        log.info("Stream consumer stopped.")


if __name__ == "__main__":
    run()
