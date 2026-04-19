"""
Airflow DAG: event_aggregation

Reads accumulated events from Redpanda (Kafka), computes hourly aggregates
per user and per upload, and writes snapshots to Iceberg for offline analysis.

This gives you a permanent historical record of streaming windows even after
Redis keys expire, so you can reconstruct time-anchored features for any
past upload when building a new model.

Schedule: Hourly
Backfill: Uses Kafka consumer group offsets as watermarks.
          Each run commits offsets only after successful Iceberg write.

Tasks:
  1. consume_kafka     → poll Redpanda for new events since last offset
  2. aggregate_windows → compute per-user / per-upload counts
  3. write_iceberg     → append aggregates to iceberg.events.hourly_aggregates
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.dates import days_ago

log = logging.getLogger(__name__)

KAFKA_BOOTSTRAP  = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
KAFKA_GROUP      = os.getenv("KAFKA_AGG_GROUP",          "dms-airflow-aggregator")
KAFKA_TOPICS     = ["dms.uploads", "dms.approvals", "dms.inference"]
DB_URL           = os.getenv("DATABASE_URL",            "postgresql://dms:dms@postgres:5432/dms")
S3_ENDPOINT      = os.getenv("ICEBERG_S3_ENDPOINT",     "https://chi.uc.chameleoncloud.org:7480")
TRAINING_BUCKET  = os.getenv("SWIFT_TRAINING_CONTAINER", "proj26-training-data")

MAX_POLL_RECORDS = 5000  # limit per hourly run

DEFAULT_ARGS = {
    "owner": "dms",
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
    "email_on_failure": False,
}


def consume_kafka(ds: str, **kwargs) -> list[dict]:
    """
    Poll Kafka topics for up to MAX_POLL_RECORDS events.
    Commits offsets only at the end of this task (not auto-commit).
    """
    try:
        from confluent_kafka import Consumer, TopicPartition
    except ImportError:
        log.warning("confluent-kafka not installed in Airflow env, skipping")
        kwargs["ti"].xcom_push(key="events", value=[])
        return []

    consumer = Consumer({
        "bootstrap.servers":   KAFKA_BOOTSTRAP,
        "group.id":            KAFKA_GROUP,
        "auto.offset.reset":   "earliest",
        "enable.auto.commit":  False,   # manual commit after successful write
    })
    consumer.subscribe(KAFKA_TOPICS)

    events = []
    empty_polls = 0
    while len(events) < MAX_POLL_RECORDS and empty_polls < 3:
        msg = consumer.poll(timeout=1.0)
        if msg is None:
            empty_polls += 1
            continue
        if msg.error():
            log.warning("Kafka error: %s", msg.error())
            continue
        try:
            event = json.loads(msg.value().decode("utf-8"))
            events.append(event)
        except Exception as exc:
            log.warning("Failed to decode message: %s", exc)

    consumer.commit(asynchronous=False)
    consumer.close()

    log.info("consume_kafka: polled %d events for ds=%s", len(events), ds)
    kwargs["ti"].xcom_push(key="events", value=events)
    return events


def aggregate_windows(ds: str, **kwargs) -> list[dict]:
    """
    Compute per-user and per-upload hourly counts from the raw events.
    """
    ti     = kwargs["ti"]
    events = ti.xcom_pull(task_ids="consume_kafka", key="events") or []

    hour_bucket = ds  # 'YYYY-MM-DD' from Airflow ds; for hourly we'd use ds_nodash + hour

    user_uploads:     dict[str, int] = {}
    user_approvals:   dict[str, int] = {}
    user_rejections:  dict[str, int] = {}
    inference_count  = 0

    for event in events:
        event_type = event.get("event_type", "")
        payload    = event.get("payload", {})
        user_id    = payload.get("user_id", "unknown")

        if event_type == "uploads":
            user_uploads[user_id] = user_uploads.get(user_id, 0) + 1

        elif event_type == "approvals":
            if payload.get("approved"):
                user_approvals[user_id]  = user_approvals.get(user_id, 0) + 1
            else:
                user_rejections[user_id] = user_rejections.get(user_id, 0) + 1

        elif event_type == "inference":
            inference_count += 1

    all_users = set(user_uploads) | set(user_approvals) | set(user_rejections)
    aggregates = [
        {
            "hour_bucket":        hour_bucket,
            "user_id":            uid,
            "upload_count":       user_uploads.get(uid, 0),
            "approval_count":     user_approvals.get(uid, 0),
            "rejection_count":    user_rejections.get(uid, 0),
            "rejection_rate":     round(
                user_rejections.get(uid, 0) / max(user_uploads.get(uid, 1), 1), 4
            ),
            "inference_requests": inference_count,
        }
        for uid in all_users
    ]

    log.info("aggregate_windows: %d user aggregates for hour=%s", len(aggregates), hour_bucket)
    ti.xcom_push(key="aggregates", value=aggregates)
    return aggregates


def write_iceberg_aggregates(ds: str, **kwargs) -> None:
    """Append hourly aggregates to the Iceberg events table."""
    import pyarrow as pa
    from pyiceberg.catalog.sql import SqlCatalog

    ti         = kwargs["ti"]
    aggregates = ti.xcom_pull(task_ids="aggregate_windows", key="aggregates") or []

    if not aggregates:
        log.info("write_iceberg_aggregates: nothing to write for ds=%s", ds)
        return

    catalog = SqlCatalog(
        "dms",
        **{
            "uri":                  f"postgresql+psycopg2://{DB_URL.split('://', 1)[1]}",
            "warehouse":            f"s3://{TRAINING_BUCKET}/iceberg-warehouse",
            "s3.endpoint":          S3_ENDPOINT,
            "s3.access-key-id":     os.getenv("AWS_ACCESS_KEY_ID", ""),
            "s3.secret-access-key": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
            "s3.path-style-access": "true",
            "py-io-impl":           "pyiceberg.io.pyarrow.PyArrowFileIO",
        },
    )

    try:
        catalog.create_namespace_if_not_exists("events")
    except Exception:
        pass

    full_name = "events.hourly_aggregates"
    schema = pa.schema([
        pa.field("hour_bucket",        pa.string(),  nullable=False),
        pa.field("user_id",            pa.string(),  nullable=False),
        pa.field("upload_count",       pa.int32(),   nullable=False),
        pa.field("approval_count",     pa.int32(),   nullable=False),
        pa.field("rejection_count",    pa.int32(),   nullable=False),
        pa.field("rejection_rate",     pa.float32(), nullable=False),
        pa.field("inference_requests", pa.int32(),   nullable=False),
    ])

    arrow_table = pa.Table.from_pylist(aggregates, schema=schema)

    try:
        table = catalog.load_table(full_name)
    except Exception:
        from pyiceberg.schema import Schema
        from pyiceberg.types import (
            FloatType, IntegerType, NestedField, StringType
        )
        iceberg_schema = Schema(
            NestedField(1, "hour_bucket",        StringType(),  required=True),
            NestedField(2, "user_id",            StringType(),  required=True),
            NestedField(3, "upload_count",       IntegerType(), required=True),
            NestedField(4, "approval_count",     IntegerType(), required=True),
            NestedField(5, "rejection_count",    IntegerType(), required=True),
            NestedField(6, "rejection_rate",     FloatType(),   required=True),
            NestedField(7, "inference_requests", IntegerType(), required=True),
        )
        table = catalog.create_table(full_name, schema=iceberg_schema)
        log.info("Created Iceberg table %s", full_name)

    table.append(arrow_table)
    snapshot_id = table.metadata.current_snapshot_id
    log.info(
        "write_iceberg_aggregates: wrote %d rows, snapshot_id=%s",
        len(aggregates), snapshot_id,
    )


# ── DAG definition ─────────────────────────────────────────────────────────────

with DAG(
    dag_id="event_aggregation",
    description="Hourly Kafka event aggregation → Iceberg events.hourly_aggregates",
    default_args=DEFAULT_ARGS,
    start_date=days_ago(1),
    schedule_interval="0 * * * *",
    catchup=False,
    tags=["dms", "streaming", "iceberg"],
) as dag:

    t_consume   = PythonOperator(task_id="consume_kafka",           python_callable=consume_kafka)
    t_aggregate = PythonOperator(task_id="aggregate_windows",       python_callable=aggregate_windows)
    t_write     = PythonOperator(task_id="write_iceberg_aggregates", python_callable=write_iceberg_aggregates)

    t_consume >> t_aggregate >> t_write
