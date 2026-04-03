import io
import json
from datetime import datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq

from dms.celery_app import celery_app
from dms.config import settings
from dms.db import SessionLocal
from dms.models import Dataset, DatasetItem, DatasetVersion, Job, JobStatus, Object, Upload, UploadStatus
from dms.storage import copy_object, put_bytes, put_text, write_version_meta


def _set_job_state(db, job_id: int, status: JobStatus, message: str | None = None) -> None:
    job = db.get(Job, job_id)
    if not job:
        return
    job.status = status
    job.message = message
    db.commit()


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

        table = pa.Table.from_pylist(rows)
        buffer = io.BytesIO()
        pq.write_table(table, buffer)

        manifest_key = f"versions/{version}/manifest.parquet"
        put_bytes(
            settings.swift_training_container,
            manifest_key,
            buffer.getvalue(),
            "application/octet-stream",
        )
        meta_key = write_version_meta(version=version, dataset_name=dataset.name, object_count=len(rows))

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
        _set_job_state(db, job_id, JobStatus.succeeded, f"published {version} with {len(rows)} items")
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
            "reports/daily_integrity.json",
            json.dumps(report, indent=2),
            "application/json",
        )
    finally:
        db.close()
