"""
Batch pipeline — Prefect flow.

Compiles a versioned training + evaluation dataset from production data in
Chameleon Swift.  Demonstrates:
  - Candidate selection (CSV ↔ image matching, deduplication, size filter)
  - Deterministic train/val/test split by recipe identity (no leakage)
  - Synthetic data expansion via Albumentations (only if dataset < 5 GB)
  - Soda data quality gate before publishing
  - Parquet manifest + meta.json versioned in Swift

Usage (run on the server):
  python -m pipelines.training_pipeline --version v2 --dataset-id 1

Or trigger via API:
  POST /pipelines/training  {"version": "v2", "dataset_id": 1}
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prefect tasks
# ---------------------------------------------------------------------------

def _prefect_available() -> bool:
    try:
        import prefect  # noqa: F401
        return True
    except ImportError:
        return False


if _prefect_available():
    from prefect import flow, task, get_run_logger
else:
    # Fallback decorators if prefect is not installed
    def task(fn=None, **_):
        return fn if fn else lambda f: f

    def flow(fn=None, **_):
        return fn if fn else lambda f: f

    def get_run_logger():
        return logging.getLogger(__name__)


@task(name="download-kaggle-dataset", retries=2, retry_delay_seconds=30)
def step_download_kaggle(dataset_slug: str, swift_prefix: str) -> str:
    logger = get_run_logger()
    logger.info("Downloading Kaggle dataset: %s", dataset_slug)

    import os
    from pathlib import Path

    import kagglehub
    from dms.config import settings
    from dms.storage import ensure_container, put_file_path

    os.environ.setdefault("KAGGLE_USERNAME", settings.kaggle_username or "")
    os.environ.setdefault("KAGGLE_KEY", settings.kaggle_key or "")

    local_path = Path(kagglehub.dataset_download(dataset_slug))
    logger.info("Downloaded to %s", local_path)

    container = settings.swift_training_container
    ensure_container(container)

    uploaded = 0
    for f in sorted(local_path.rglob("*")):
        if f.is_file():
            rel = f.relative_to(local_path)
            key = f"{swift_prefix.strip('/')}/{rel}"
            put_file_path(container, key, f)
            uploaded += 1

    logger.info("Uploaded %d files to Swift under %s", uploaded, swift_prefix)
    return local_path.as_posix()


@task(name="compile-dataset")
def step_compile(version: str) -> tuple:
    logger = get_run_logger()
    logger.info("Compiling dataset version %s", version)

    from dms.batch_pipeline import compile_dataset
    from dms.config import settings

    records, stats = compile_dataset(
        container=settings.swift_training_container,
        version=version,
    )
    logger.info(
        "Compiled: matched=%d train=%d val=%d test=%d",
        stats.matched, stats.train, stats.val, stats.test,
    )
    return records, stats


@task(name="soda-quality-check")
def step_soda_check(records: list) -> None:
    logger = get_run_logger()

    if not records:
        raise ValueError("Quality check failed: zero records after candidate selection")

    missing_keys = [r for r in records if not getattr(r, "object_key", None)]
    if missing_keys:
        raise ValueError(f"Quality check failed: {len(missing_keys)} records missing object_key")

    titles = [getattr(r, "title", "") for r in records]
    if len(set(titles)) < len(titles) * 0.9:
        raise ValueError("Quality check failed: more than 10% duplicate titles")

    splits = {getattr(r, "split", "") for r in records}
    for required in ("train", "val", "test"):
        if required not in splits:
            raise ValueError(f"Quality check failed: split '{required}' is missing")

    logger.info("Soda-style quality checks passed: %d records, splits=%s", len(records), splits)


@task(name="write-manifest")
def step_write_manifest(records: list, stats, version: str, enable_augmentation: bool = True) -> tuple:
    logger = get_run_logger()
    logger.info("Writing versioned manifest (augmentation=%s)", enable_augmentation)

    from dms.batch_pipeline import write_manifest
    from dms.config import settings

    manifest_key, meta_key = write_manifest(
        records, stats,
        container=settings.swift_training_container,
        version=version,
        enable_augmentation=enable_augmentation,
    )
    logger.info("Manifest written: %s", manifest_key)
    return manifest_key, meta_key


@task(name="register-dataset-version")
def step_register_version(dataset_id: int, version: str, manifest_key: str, meta_key: str) -> int:
    logger = get_run_logger()

    from dms.db import SessionLocal
    from dms.models import DatasetVersion

    db = SessionLocal()
    try:
        dv = DatasetVersion(
            dataset_id=dataset_id,
            version=version,
            manifest_key=manifest_key,
            meta_key=meta_key,
        )
        db.add(dv)
        db.commit()
        db.refresh(dv)
        logger.info("Registered DatasetVersion id=%d version=%s", dv.id, version)
        return dv.id
    finally:
        db.close()


@task(name="trigger-qdrant-index-build")
def step_build_qdrant_index(dataset_version_id: int, manifest_key: str) -> str:
    logger = get_run_logger()
    logger.info("Triggering Qdrant index build from %s", manifest_key)

    from dms.db import SessionLocal
    from dms.models import Job, JobStatus
    from dms.tasks import build_qdrant_index
    from datetime import datetime
    import json

    db = SessionLocal()
    try:
        job = Job(
            kind="build_qdrant_index",
            status=JobStatus.queued,
            payload_json=json.dumps({"manifest_key": manifest_key,
                                     "dataset_version_id": dataset_version_id}),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        task_result = build_qdrant_index.delay(job.id, manifest_key)
        job.celery_task_id = task_result.id
        db.commit()

        logger.info("Qdrant index build job queued: job_id=%d", job.id)
        return task_result.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

@flow(name="dms-training-pipeline", log_prints=True)
def training_pipeline(
    version: str = "v1",
    dataset_id: int = 1,
    dataset_slug: str = "pes12017000148/food-ingredients-and-recipe-dataset-with-images",
    swift_prefix: str = "recipe1m/kaggle-food-images",
    skip_download: bool = False,
    enable_augmentation: bool = True,
) -> dict:
    """
    End-to-end batch pipeline:
      1. Download Kaggle dataset → Swift  (skipped if skip_download=True)
      2. Compile: CSV × image matching, dedup, size filter, deterministic split
      3. Quality gate: Soda-style checks
      4. Write versioned Parquet manifest + meta.json (+ augmented images)
      5. Register DatasetVersion in Postgres
      6. Trigger Qdrant index build (online feature computation path)
    """
    if not skip_download:
        step_download_kaggle(dataset_slug, swift_prefix)

    records, stats = step_compile(version)
    step_soda_check(records)
    manifest_key, meta_key = step_write_manifest(records, stats, version, enable_augmentation)
    dv_id = step_register_version(dataset_id, version, manifest_key, meta_key)
    task_id = step_build_qdrant_index(dv_id, manifest_key)

    return {
        "version": version,
        "dataset_version_id": dv_id,
        "manifest_key": manifest_key,
        "total_records": stats.matched,
        "augmented_records": stats.augmented,
        "qdrant_index_task_id": task_id,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run DMS training pipeline")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--dataset-id", type=int, default=1)
    parser.add_argument("--dataset-slug", default="pes12017000148/food-ingredients-and-recipe-dataset-with-images")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--no-augmentation", action="store_true")
    args = parser.parse_args()

    result = training_pipeline(
        version=args.version,
        dataset_id=args.dataset_id,
        dataset_slug=args.dataset_slug,
        skip_download=args.skip_download,
        enable_augmentation=not args.no_augmentation,
    )
    print("Pipeline complete:", result)
