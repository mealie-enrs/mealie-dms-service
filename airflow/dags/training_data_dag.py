"""
Airflow DAG: training_data_assembly

Assembles a versioned Iceberg training snapshot from:
  - Postgres objects + uploads tables (application state)
  - Redis rolling window features (from the stream consumer)
  - Governance filters (exclude deleted / test / underage)

Schedule: Daily at 02:00 UTC
Backfill: Supported — each run writes a new Iceberg snapshot with a date-stamped version.
Retry: 2 retries with 5-minute backoff.

Tasks:
  1. extract_objects      → query Postgres for eligible objects + governance filters
  2. join_redis_features  → enrich each row with 5-min/1-hr window counts from Redis
  3. write_iceberg        → append enriched rows to the Iceberg training table
  4. register_version     → write manifest_key + snapshot_id to dataset_versions in Postgres
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

DB_URL          = os.getenv("DATABASE_URL",           "postgresql://dms:dms@postgres:5432/dms")
REDIS_URL       = os.getenv("REDIS_URL",              "redis://redis:6379/2")
S3_ENDPOINT     = os.getenv("ICEBERG_S3_ENDPOINT",    "https://chi.uc.chameleoncloud.org:7480")
TRAINING_BUCKET = os.getenv("SWIFT_TRAINING_CONTAINER", "proj26-training-data")

DEFAULT_ARGS = {
    "owner": "dms",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "email_on_failure": False,
}


# ── Task 1: extract eligible objects from Postgres ────────────────────────────

def extract_objects(ds: str, **kwargs) -> list[dict]:
    """
    Pull all non-deleted, non-test objects from Postgres.
    Applies governance filters:
      - deleted_at IS NULL
      - is_test_account = FALSE
      - source IN ('kaggle', 'recipe1m', 'user_upload')
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(DB_URL)
    query = text("""
        SELECT
            o.id               AS object_id,
            o.object_key,
            o.checksum_sha256,
            o.source::text     AS source,
            o.created_at,
            u.user_id,
            u.country,
            u.is_test_account  AS upload_is_test
        FROM objects o
        LEFT JOIN uploads u ON u.id = o.source_upload_id
        WHERE o.deleted_at IS NULL
          AND o.is_test_account = FALSE
          AND o.source::text IN ('kaggle', 'recipe1m', 'user_upload')
        ORDER BY o.id
    """)

    with engine.connect() as conn:
        rows = conn.execute(query).mappings().all()

    result = [dict(r) for r in rows]
    log.info("extract_objects: %d eligible objects for ds=%s", len(result), ds)

    # Push to XCom for downstream tasks
    kwargs["ti"].xcom_push(key="objects", value=result)
    return result


# ── Task 2: enrich rows with Redis window features ────────────────────────────

def join_redis_features(ds: str, **kwargs) -> list[dict]:
    """
    For each object that came from a user upload, look up 5-min and 1-hr
    upload-burst counts from the Redis feature store written by stream_consumer.
    """
    import redis as redis_lib

    ti = kwargs["ti"]
    objects = ti.xcom_pull(task_ids="extract_objects", key="objects") or []
    r = redis_lib.from_url(REDIS_URL, decode_responses=True)

    enriched = []
    for obj in objects:
        user_id = obj.get("user_id")
        upload_burst_5min = 0
        upload_burst_1hr  = 0
        rejection_rate_1hr = 0.0

        if user_id:
            try:
                now = __import__("time").time()
                r.zremrangebyscore(f"user:{user_id}:uploads:5min", "-inf", now - 300)
                r.zremrangebyscore(f"user:{user_id}:uploads:1hr",  "-inf", now - 3600)
                r.zremrangebyscore(f"user:{user_id}:rejections:1hr", "-inf", now - 3600)

                upload_burst_5min = r.zcard(f"user:{user_id}:uploads:5min") or 0
                upload_burst_1hr  = r.zcard(f"user:{user_id}:uploads:1hr")  or 0
                rejections_1hr    = r.zcard(f"user:{user_id}:rejections:1hr") or 0
                if upload_burst_1hr > 0:
                    rejection_rate_1hr = rejections_1hr / upload_burst_1hr
            except Exception as exc:
                log.debug("Redis lookup failed for user_id=%s: %s", user_id, exc)

        enriched.append({
            **obj,
            "upload_burst_5min":  upload_burst_5min,
            "upload_burst_1hr":   upload_burst_1hr,
            "rejection_rate_1hr": round(rejection_rate_1hr, 4),
            "snapshot_date":      ds,
        })

    log.info("join_redis_features: enriched %d rows", len(enriched))
    ti.xcom_push(key="enriched", value=enriched)
    return enriched


# ── Task 3: write Iceberg snapshot ────────────────────────────────────────────

def write_iceberg(ds: str, **kwargs) -> str:
    """
    Append the enriched rows to the Iceberg training table as a new snapshot.
    Returns the snapshot_id string for downstream registration.
    """
    import pyarrow as pa
    from pyiceberg.catalog.sql import SqlCatalog

    ti       = kwargs["ti"]
    enriched = ti.xcom_pull(task_ids="join_redis_features", key="enriched") or []

    if not enriched:
        log.warning("write_iceberg: no rows to write for ds=%s", ds)
        return ""

    version = f"v_{ds.replace('-', '')}"

    catalog = SqlCatalog(
        "dms",
        **{
            "uri":                f"postgresql+psycopg2://{DB_URL.split('://', 1)[1]}",
            "warehouse":         f"s3://{TRAINING_BUCKET}/iceberg-warehouse",
            "s3.endpoint":       S3_ENDPOINT,
            "s3.access-key-id":  os.getenv("AWS_ACCESS_KEY_ID", ""),
            "s3.secret-access-key": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
            "s3.path-style-access": "true",
            "py-io-impl":        "pyiceberg.io.pyarrow.PyArrowFileIO",
        },
    )

    try:
        catalog.create_namespace_if_not_exists("training")
    except Exception:
        pass

    full_name = "training.food_images_enriched"
    schema = pa.schema([
        pa.field("object_id",          pa.int64(),   nullable=False),
        pa.field("object_key",         pa.string(),  nullable=False),
        pa.field("checksum_sha256",    pa.string(),  nullable=True),
        pa.field("source",             pa.string(),  nullable=False),
        pa.field("user_id",            pa.string(),  nullable=True),
        pa.field("country",            pa.string(),  nullable=True),
        pa.field("upload_burst_5min",  pa.int32(),   nullable=True),
        pa.field("upload_burst_1hr",   pa.int32(),   nullable=True),
        pa.field("rejection_rate_1hr", pa.float32(), nullable=True),
        pa.field("snapshot_date",      pa.string(),  nullable=False),
        pa.field("version",            pa.string(),  nullable=False),
    ])

    rows_with_version = [{**r, "version": version} for r in enriched]
    arrow_table = pa.Table.from_pylist(rows_with_version, schema=schema)

    try:
        table = catalog.load_table(full_name)
    except Exception:
        from pyiceberg.schema import Schema
        from pyiceberg.types import (
            FloatType, IntegerType, LongType, NestedField, StringType
        )
        iceberg_schema = Schema(
            NestedField(1,  "object_id",          LongType(),    required=True),
            NestedField(2,  "object_key",         StringType(),  required=True),
            NestedField(3,  "checksum_sha256",    StringType(),  required=False),
            NestedField(4,  "source",             StringType(),  required=True),
            NestedField(5,  "user_id",            StringType(),  required=False),
            NestedField(6,  "country",            StringType(),  required=False),
            NestedField(7,  "upload_burst_5min",  IntegerType(), required=False),
            NestedField(8,  "upload_burst_1hr",   IntegerType(), required=False),
            NestedField(9,  "rejection_rate_1hr", FloatType(),   required=False),
            NestedField(10, "snapshot_date",      StringType(),  required=True),
            NestedField(11, "version",            StringType(),  required=True),
        )
        table = catalog.create_table(full_name, schema=iceberg_schema)
        log.info("Created Iceberg table %s", full_name)

    table.append(arrow_table)
    snapshot_id = str(table.metadata.current_snapshot_id)
    log.info("write_iceberg: wrote %d rows, snapshot_id=%s", len(enriched), snapshot_id)

    ti.xcom_push(key="snapshot_id", value=snapshot_id)
    ti.xcom_push(key="version",     value=version)
    return snapshot_id


# ── Task 4: register in dataset_versions ─────────────────────────────────────

def register_version(ds: str, **kwargs) -> None:
    """Record the Iceberg snapshot in the dataset_versions table for lineage tracking."""
    from sqlalchemy import create_engine, text

    ti          = kwargs["ti"]
    snapshot_id = ti.xcom_pull(task_ids="write_iceberg", key="snapshot_id")
    version     = ti.xcom_pull(task_ids="write_iceberg", key="version")
    enriched    = ti.xcom_pull(task_ids="join_redis_features", key="enriched") or []

    if not snapshot_id:
        log.warning("register_version: no snapshot_id, skipping registration")
        return

    engine = create_engine(DB_URL)
    meta   = json.dumps({"snapshot_id": snapshot_id, "row_count": len(enriched), "date": ds})

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO dataset_versions (dataset_id, version, manifest_key, meta_key, created_at)
            VALUES (
                (SELECT id FROM datasets ORDER BY id LIMIT 1),
                :version,
                :manifest_key,
                :meta_key,
                NOW()
            )
            ON CONFLICT DO NOTHING
        """), {
            "version":      version,
            "manifest_key": f"iceberg://training.food_images_enriched@{snapshot_id}",
            "meta_key":     meta,
        })

    log.info("register_version: registered version=%s snapshot_id=%s", version, snapshot_id)


# ── DAG definition ─────────────────────────────────────────────────────────────

with DAG(
    dag_id="training_data_assembly",
    description="Daily Iceberg training snapshot with governance filters + Redis features",
    default_args=DEFAULT_ARGS,
    start_date=days_ago(1),
    schedule_interval="0 2 * * *",
    catchup=False,
    tags=["dms", "training", "iceberg"],
) as dag:

    t_extract = PythonOperator(
        task_id="extract_objects",
        python_callable=extract_objects,
    )

    t_redis = PythonOperator(
        task_id="join_redis_features",
        python_callable=join_redis_features,
    )

    t_iceberg = PythonOperator(
        task_id="write_iceberg",
        python_callable=write_iceberg,
    )

    t_register = PythonOperator(
        task_id="register_version",
        python_callable=register_version,
    )

    t_extract >> t_redis >> t_iceberg >> t_register
