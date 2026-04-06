import io
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from dms.celery_app import celery_app
from dms.config import settings
from dms.db import SessionLocal
from dms.models import Dataset, DatasetItem, DatasetVersion, Job, JobStatus, Object, Upload, UploadStatus
from dms.recipe1m import iter_sample_image_urls, upload_curated_object, upload_raw_sample
from dms.storage import copy_object, ensure_container, put_bytes, put_file_path, put_text, write_version_meta

REPORT_PREFIX = "recipe1m_reports"
VERSION_PREFIX = "recipe1m_versions"


def _set_job_state(db, job_id: int, status: JobStatus, message: str | None = None) -> None:
    job = db.get(Job, job_id)
    if not job:
        return
    job.status = status
    job.message = message
    db.commit()


def _run_publish_quality_checks(rows: list[dict]) -> None:
    if not rows:
        raise ValueError("quality check failed: empty manifest rows")

    missing_keys = [row for row in rows if not row.get("object_key")]
    if missing_keys:
        raise ValueError(f"quality check failed: {len(missing_keys)} rows missing object_key")

    checksums = [row.get("checksum") for row in rows if row.get("checksum")]
    duplicate_count = len(checksums) - len(set(checksums))
    if duplicate_count > 0:
        raise ValueError(f"quality check failed: duplicate checksum count={duplicate_count}")

    # Optional Soda integration for bonus data-quality gating.
    if os.getenv("ENABLE_SODA_CHECKS", "false").lower() == "true":
        config_file = os.getenv("SODA_CONFIGURATION_FILE", "quality/soda/configuration.yml")
        checks_file = os.getenv("SODA_CHECKS_FILE", "quality/soda/checks.yml")
        if not (os.path.exists(config_file) and os.path.exists(checks_file)):
            raise ValueError("Soda checks enabled but configuration/checks files are missing")

        result = subprocess.run(
            ["soda", "scan", "-d", "dms", "-c", config_file, checks_file],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            raise ValueError(f"Soda checks failed: {stderr or stdout}")


def _publish_dataset_version_records(
    db,
    *,
    dataset_id: int,
    dataset_name: str,
    version: str,
    objects: list[Object],
) -> tuple[str, str, int]:
    rows = []
    for obj in objects:
        rows.append(
            {
                "object_key": obj.object_key,
                "label": None,
                "split": "train",
                "width": obj.width,
                "height": obj.height,
                "checksum": obj.checksum_sha256,
                "source_upload_id": obj.source_upload_id,
            }
        )

    _run_publish_quality_checks(rows)

    table = pa.Table.from_pylist(rows)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)

    manifest_key = f"{VERSION_PREFIX}/{version}/manifest.parquet"
    put_bytes(
        settings.swift_training_container,
        manifest_key,
        buffer.getvalue(),
        "application/octet-stream",
    )
    meta_key = write_version_meta(version=version, dataset_name=dataset_name, object_count=len(rows))

    dataset_version = DatasetVersion(
        dataset_id=dataset_id,
        version=version,
        manifest_key=manifest_key,
        meta_key=meta_key,
    )
    db.add(dataset_version)
    db.flush()

    for obj in objects:
        db.add(
            DatasetItem(
                dataset_version_id=dataset_version.id,
                object_id=obj.id,
                label=None,
                split="train",
            )
        )

    db.commit()
    return manifest_key, meta_key, len(rows)


@celery_app.task(name="dms.tasks.process_upload_approval")
def process_upload_approval(job_id: int, upload_id: int, approve: bool) -> None:
    db = SessionLocal()
    try:
        _set_job_state(db, job_id, JobStatus.running)
        upload = db.get(Upload, upload_id)
        if not upload:
            _set_job_state(db, job_id, JobStatus.failed, "upload not found")
            return

        if not approve:
            upload.status = UploadStatus.rejected
            upload.updated_at = datetime.utcnow()
            db.commit()
            _set_job_state(db, job_id, JobStatus.succeeded, "upload rejected")
            return

        # In production, hash should come from upload stream bytes or object metadata.
        pseudo_digest = f"{upload.id:064x}"[-64:]
        ext = (upload.original_filename.split(".")[-1] or "jpg").lower()
        curated_key = f"objects/sha256/{pseudo_digest[0:2]}/{pseudo_digest[2:4]}/{pseudo_digest}.{ext}"

        copy_object(
            settings.swift_user_uploads_container,
            upload.incoming_key,
            settings.swift_training_container,
            curated_key,
        )

        upload.status = UploadStatus.approved
        upload.curated_object_key = curated_key
        upload.checksum_sha256 = pseudo_digest
        upload.updated_at = datetime.utcnow()

        obj = Object(
            object_key=curated_key,
            checksum_sha256=pseudo_digest,
            mime_type=upload.mime_type,
            width=upload.width,
            height=upload.height,
            source_upload_id=upload.id,
        )
        db.add(obj)
        db.commit()

        _set_job_state(db, job_id, JobStatus.succeeded, "upload approved and promoted")
    except Exception as exc:
        db.rollback()
        _set_job_state(db, job_id, JobStatus.failed, f"failed: {exc}")
        raise
    finally:
        db.close()


@celery_app.task(name="dms.tasks.publish_dataset_version")
def publish_dataset_version(job_id: int, dataset_id: int, version: str, object_ids: list[int]) -> None:
    db = SessionLocal()
    try:
        _set_job_state(db, job_id, JobStatus.running)

        dataset = db.get(Dataset, dataset_id)
        if not dataset:
            _set_job_state(db, job_id, JobStatus.failed, "dataset not found")
            return

        objects = db.query(Object).filter(Object.id.in_(object_ids)).all()
        if not objects:
            _set_job_state(db, job_id, JobStatus.failed, "no objects selected")
            return

        _, _, row_count = _publish_dataset_version_records(
            db,
            dataset_id=dataset_id,
            dataset_name=dataset.name,
            version=version,
            objects=objects,
        )
        _set_job_state(db, job_id, JobStatus.succeeded, f"published {version} with {row_count} items")
    except Exception as exc:
        db.rollback()
        _set_job_state(db, job_id, JobStatus.failed, f"failed: {exc}")
        raise
    finally:
        db.close()


@celery_app.task(name="dms.tasks.ingest_recipe1m_sample")
def ingest_recipe1m_sample(
    job_id: int,
    dataset_id: int,
    manifest_source: str,
    sample_size: int = 1000,
    raw_prefix: str = "raw/recipe1m",
    target_container: str | None = None,
    auto_publish_version: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        _set_job_state(db, job_id, JobStatus.running)
        dataset = db.get(Dataset, dataset_id)
        if not dataset:
            _set_job_state(db, job_id, JobStatus.failed, "dataset not found")
            return

        container = target_container or settings.swift_training_container
        run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        run_prefix = f"{raw_prefix.rstrip('/')}/{run_id}"

        created_object_ids: list[int] = []
        skipped_existing = 0
        failures = 0

        for idx, image_url in enumerate(iter_sample_image_urls(manifest_source, sample_size), start=1):
            try:
                raw_key, raw_bytes = upload_raw_sample(
                    image_url=image_url,
                    container=container,
                    raw_prefix=run_prefix,
                    item_idx=idx,
                )
                object_key, checksum = upload_curated_object(
                    raw_bytes=raw_bytes,
                    container=container,
                    image_url=image_url,
                )

                existing = db.query(Object).filter(Object.object_key == object_key).one_or_none()
                if existing:
                    created_object_ids.append(existing.id)
                    skipped_existing += 1
                    continue

                obj = Object(
                    object_key=object_key,
                    checksum_sha256=checksum,
                    mime_type="image/*",
                    width=None,
                    height=None,
                    source_upload_id=None,
                )
                db.add(obj)
                db.commit()
                db.refresh(obj)
                created_object_ids.append(obj.id)
            except Exception:
                failures += 1

        summary = {
            "dataset_id": dataset_id,
            "manifest_source": manifest_source,
            "sample_size_requested": sample_size,
            "container": container,
            "run_prefix": run_prefix,
            "objects_registered": len(created_object_ids),
            "existing_reused": skipped_existing,
            "failed_downloads": failures,
            "auto_publish_version": auto_publish_version,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        put_text(container, f"{run_prefix}/ingest_summary.json", json.dumps(summary, indent=2), "application/json")

        if auto_publish_version and created_object_ids:
            objects = db.query(Object).filter(Object.id.in_(created_object_ids)).all()
            _publish_dataset_version_records(
                db,
                dataset_id=dataset.id,
                dataset_name=dataset.name,
                version=auto_publish_version,
                objects=objects,
            )

        _set_job_state(
            db,
            job_id,
            JobStatus.succeeded,
            f"ingested={len(created_object_ids)} existing={skipped_existing} failed={failures}",
        )
    except Exception as exc:
        db.rollback()
        _set_job_state(db, job_id, JobStatus.failed, f"failed: {exc}")
        raise
    finally:
        db.close()


@celery_app.task(name="dms.tasks.download_kaggle_dataset")
def download_kaggle_dataset(
    job_id: int,
    dataset_slug: str = "pes12017000148/food-ingredients-and-recipe-dataset-with-images",
    upload_to_swift: bool = True,
    swift_subpath: str = "",
) -> None:
    log = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        _set_job_state(db, job_id, JobStatus.running, f"downloading kaggle dataset {dataset_slug}")

        if not settings.kaggle_username or not settings.kaggle_key:
            _set_job_state(db, job_id, JobStatus.failed, "KAGGLE_USERNAME / KAGGLE_KEY not set on this worker")
            return

        os.environ.setdefault("KAGGLE_USERNAME", settings.kaggle_username)
        os.environ.setdefault("KAGGLE_KEY", settings.kaggle_key)

        import kagglehub  # noqa: E402 — deferred so env vars are set first

        log.info("kagglehub: downloading %s", dataset_slug)
        local_path = kagglehub.dataset_download(dataset_slug)
        local_path = Path(local_path)
        log.info("kagglehub: downloaded to %s", local_path)

        files = sorted(f for f in local_path.rglob("*") if f.is_file())
        log.info("kagglehub: %d files found", len(files))

        uploaded = 0
        if upload_to_swift:
            container = settings.swift_training_container
            ensure_container(container)
            prefix = settings.swift_recipe1m_prefix.strip("/")
            sub = swift_subpath.strip("/")
            base_key = "/".join(p for p in (prefix, sub) if p)

            for f in files:
                rel = f.relative_to(local_path)
                key = f"{base_key}/{rel}" if base_key else str(rel)
                log.info("uploading %s -> %s/%s", f.name, container, key)
                put_file_path(container, key, f)
                uploaded += 1

        msg = f"downloaded={len(files)} local_path={local_path}"
        if upload_to_swift:
            msg += f" uploaded={uploaded} container={settings.swift_training_container}"
        _set_job_state(db, job_id, JobStatus.succeeded, msg)
    except Exception as exc:
        db.rollback()
        _set_job_state(db, job_id, JobStatus.failed, f"failed: {exc}")
        raise
    finally:
        db.close()


@celery_app.task(name="dms.tasks.compile_training_dataset")
def compile_training_dataset(
    job_id: int,
    dataset_id: int,
    version: str = "v1",
    container: str | None = None,
) -> None:
    from dms.batch_pipeline import compile_dataset, write_manifest

    db = SessionLocal()
    try:
        _set_job_state(db, job_id, JobStatus.running, "compiling training dataset")

        dataset = db.get(Dataset, dataset_id)
        if not dataset:
            _set_job_state(db, job_id, JobStatus.failed, "dataset not found")
            return

        target = container or settings.swift_training_container
        records, stats = compile_dataset(container=target, version=version)

        if not records:
            _set_job_state(db, job_id, JobStatus.failed, "no records after candidate selection")
            return

        manifest_key, meta_key = write_manifest(records, stats, container=target, version=version)

        dv = DatasetVersion(
            dataset_id=dataset_id,
            version=version,
            manifest_key=manifest_key,
            meta_key=meta_key,
        )
        db.add(dv)
        db.commit()

        msg = (
            f"version={version} records={stats.matched} "
            f"train={stats.train} val={stats.val} test={stats.test} "
            f"skipped_no_img={stats.skipped_no_image} "
            f"skipped_small={stats.skipped_too_small} "
            f"skipped_dup={stats.skipped_duplicate}"
        )
        _set_job_state(db, job_id, JobStatus.succeeded, msg)
    except Exception as exc:
        db.rollback()
        _set_job_state(db, job_id, JobStatus.failed, f"failed: {exc}")
        raise
    finally:
        db.close()


@celery_app.task(name="dms.tasks.cleanup_stale_uploads")
def cleanup_stale_uploads() -> None:
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=2)
        stale = (
            db.query(Upload)
            .filter(Upload.status == UploadStatus.incoming, Upload.created_at < cutoff)
            .all()
        )
        for upload in stale:
            upload.status = UploadStatus.quarantined
            upload.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


@celery_app.task(name="dms.tasks.daily_integrity_check")
def daily_integrity_check() -> None:
    db = SessionLocal()
    try:
        total_objects = db.query(Object).count()
        total_versions = db.query(DatasetVersion).count()
        report = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "total_objects": total_objects,
            "total_versions": total_versions,
        }
        put_text(
            settings.swift_training_container,
            f"{REPORT_PREFIX}/daily_integrity.json",
            json.dumps(report, indent=2),
            "application/json",
        )
    finally:
        db.close()
