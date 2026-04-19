"""
Iceberg lakehouse integration.

Replaces plain Parquet manifest writes with PyIceberg tables backed by
Chameleon Swift (S3-compatible endpoint on port 7480).

Benefits over plain Parquet:
  - Every write creates a new snapshot → time travel and full history.
  - GDPR row-level deletes without rebuilding the full file.
  - Schema evolution (add columns) without breaking existing readers.
  - Snapshot ID stored in model metadata → exact training data traceability.

Catalog: SQL catalog stored in Postgres (schema: iceberg_catalog).
Warehouse: Swift container via S3 API.

Usage:
    from dms.iceberg_store import IcebergStore
    store = IcebergStore()
    snapshot_id = store.write_training_snapshot(rows, version="v1")
"""
from __future__ import annotations

import logging
import os
from typing import Any

import pyarrow as pa

from dms.config import settings

log = logging.getLogger(__name__)

# Iceberg namespace and table
NAMESPACE = "training"
TABLE_NAME = "food_images"
FULL_TABLE = f"{NAMESPACE}.{TABLE_NAME}"

# Training dataset schema
TRAINING_SCHEMA = pa.schema([
    pa.field("object_key",          pa.string(),  nullable=False),
    pa.field("title",               pa.string(),  nullable=True),
    pa.field("ingredients",         pa.string(),  nullable=True),
    pa.field("cleaned_ingredients", pa.string(),  nullable=True),
    pa.field("image_name",          pa.string(),  nullable=True),
    pa.field("split",               pa.string(),  nullable=False),
    pa.field("size_bytes",          pa.int64(),   nullable=True),
    pa.field("augmented",           pa.bool_(),   nullable=False),
    pa.field("source_object_key",   pa.string(),  nullable=True),
    pa.field("source",              pa.string(),  nullable=False),  # kaggle / user_upload / recipe1m
    pa.field("version",             pa.string(),  nullable=False),
    pa.field("dataset_version_id",  pa.int64(),   nullable=True),
])


class IcebergStore:
    """
    Thin wrapper around PyIceberg for writing versioned training snapshots.

    The catalog is configured from environment variables set in docker-compose:
      ICEBERG_CATALOG_URI     → jdbc:postgresql://postgres:5432/dms
      ICEBERG_S3_ENDPOINT     → https://chi.uc.chameleoncloud.org:7480
      AWS_ACCESS_KEY_ID       → Chameleon EC2 access key
      AWS_SECRET_ACCESS_KEY   → Chameleon EC2 secret key
    """

    def __init__(self) -> None:
        self._catalog = None

    def _get_catalog(self):
        if self._catalog is not None:
            return self._catalog

        try:
            from pyiceberg.catalog.sql import SqlCatalog
        except ImportError as exc:
            raise RuntimeError(
                "PyIceberg is not installed. Add pyiceberg[s3,pyarrow,sql-postgres] to requirements."
            ) from exc

        catalog_uri = os.getenv(
            "ICEBERG_CATALOG_URI",
            f"postgresql+psycopg2://{settings.database_url.split('://', 1)[1]}"
            if "://" in settings.database_url
            else "postgresql+psycopg2://dms:dms@postgres:5432/dms",
        )

        s3_endpoint = os.getenv("ICEBERG_S3_ENDPOINT", "https://chi.uc.chameleoncloud.org:7480")
        warehouse = os.getenv(
            "ICEBERG_WAREHOUSE",
            f"s3://{settings.swift_training_container}/iceberg-warehouse",
        )

        self._catalog = SqlCatalog(
            "dms",
            **{
                "uri": catalog_uri,
                "warehouse": warehouse,
                "s3.endpoint": s3_endpoint,
                "s3.access-key-id": os.getenv("AWS_ACCESS_KEY_ID", ""),
                "s3.secret-access-key": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
                "s3.path-style-access": "true",
                "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
            },
        )
        return self._catalog

    def _ensure_table(self):
        catalog = self._get_catalog()
        try:
            catalog.create_namespace_if_not_exists(NAMESPACE)
        except Exception:
            pass

        try:
            return catalog.load_table(FULL_TABLE)
        except Exception:
            from pyiceberg.schema import Schema
            from pyiceberg.types import (
                BooleanType, IntegerType, LongType, NestedField, StringType
            )

            iceberg_schema = Schema(
                NestedField(1,  "object_key",          StringType(),  required=True),
                NestedField(2,  "title",               StringType(),  required=False),
                NestedField(3,  "ingredients",         StringType(),  required=False),
                NestedField(4,  "cleaned_ingredients", StringType(),  required=False),
                NestedField(5,  "image_name",          StringType(),  required=False),
                NestedField(6,  "split",               StringType(),  required=True),
                NestedField(7,  "size_bytes",          LongType(),    required=False),
                NestedField(8,  "augmented",           BooleanType(), required=True),
                NestedField(9,  "source_object_key",   StringType(),  required=False),
                NestedField(10, "source",              StringType(),  required=True),
                NestedField(11, "version",             StringType(),  required=True),
                NestedField(12, "dataset_version_id",  LongType(),    required=False),
            )
            table = catalog.create_table(FULL_TABLE, schema=iceberg_schema)
            log.info("Created Iceberg table %s", FULL_TABLE)
            return table

    def write_training_snapshot(
        self,
        rows: list[dict[str, Any]],
        version: str,
        dataset_version_id: int | None = None,
    ) -> int:
        """
        Append rows to the Iceberg table as a new snapshot.
        Returns the Iceberg snapshot_id for traceability.
        """
        if not rows:
            raise ValueError("Cannot write empty snapshot to Iceberg")

        # Inject version + dataset_version_id into every row
        enriched = [
            {
                **row,
                "version": version,
                "dataset_version_id": dataset_version_id,
                "source": row.get("source", "kaggle"),
                "augmented": bool(row.get("augmented", False)),
            }
            for row in rows
        ]

        table = self._ensure_table()
        arrow_table = pa.Table.from_pylist(enriched, schema=TRAINING_SCHEMA)
        table.append(arrow_table)

        snapshot_id = table.metadata.current_snapshot_id
        log.info(
            "Iceberg snapshot written: table=%s rows=%d version=%s snapshot_id=%s",
            FULL_TABLE, len(rows), version, snapshot_id,
        )
        return snapshot_id

    def delete_by_object_keys(self, object_keys: list[str]) -> None:
        """
        GDPR row-level delete: remove rows by object_key without rebuilding the table.
        Creates a new Iceberg snapshot that excludes the specified rows.
        """
        if not object_keys:
            return

        table = self._ensure_table()
        keys_sql = ", ".join(f"'{k}'" for k in object_keys)
        table.delete(f"object_key IN ({keys_sql})")
        log.info("Iceberg delete: removed %d rows, new snapshot created", len(object_keys))

    def get_snapshot_info(self) -> dict:
        """Return current snapshot metadata for model lineage tracking."""
        table = self._ensure_table()
        snap = table.metadata.current_snapshot_id
        history = [
            {"snapshot_id": s.snapshot_id, "timestamp_ms": s.timestamp_ms}
            for s in table.history()
        ]
        return {
            "table": FULL_TABLE,
            "current_snapshot_id": snap,
            "history": history[-10:],
        }


# Module-level singleton
_store: IcebergStore | None = None


def get_iceberg_store() -> IcebergStore:
    global _store
    if _store is None:
        _store = IcebergStore()
    return _store
