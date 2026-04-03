import json
import uuid
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from dms.db import Base, engine, get_db
from dms.models import Dataset, DatasetVersion, Job, JobStatus, Upload, UploadStatus
from dms.schemas import (
    ApproveUploadRequest,
    DatasetCreateRequest,
    PublishVersionRequest,
    Recipe1MSampleIngestRequest,
    UploadInitRequest,
    UploadInitResponse,
)
from dms.tasks import ingest_recipe1m_sample, process_upload_approval, publish_dataset_version

app = FastAPI(title="DMS API")


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/uploads/init", response_model=UploadInitResponse)
def init_upload(payload: UploadInitRequest, db: Session = Depends(get_db)) -> UploadInitResponse:
    upload_uuid = str(uuid.uuid4())
    incoming_key = f"incoming/{payload.user_id}/{upload_uuid}-{payload.filename}"
    upload = Upload(
        user_id=payload.user_id,
        original_filename=payload.filename,
        status=UploadStatus.incoming,
        incoming_key=incoming_key,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(upload)
    db.commit()
    db.refresh(upload)
    return UploadInitResponse(upload_id=upload.id, incoming_key=incoming_key)


@app.post("/uploads/{upload_id}/approval")
def approve_upload(upload_id: int, payload: ApproveUploadRequest, db: Session = Depends(get_db)) -> dict:
    upload = db.get(Upload, upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="upload not found")

    job = Job(
        kind="upload_approval",
        status=JobStatus.queued,
        payload_json=json.dumps({"upload_id": upload_id, "approve": payload.approve}),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    task = process_upload_approval.delay(job.id, upload_id, payload.approve)
    job.celery_task_id = task.id
    db.commit()

    return {"job_id": job.id, "task_id": task.id, "status": job.status}


@app.post("/datasets")
def create_dataset(payload: DatasetCreateRequest, db: Session = Depends(get_db)) -> dict:
    dataset = Dataset(name=payload.name, description=payload.description)
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return {"dataset_id": dataset.id, "name": dataset.name}


@app.get("/datasets/{dataset_id}/versions")
def list_versions(dataset_id: int, db: Session = Depends(get_db)) -> dict:
    versions = (
        db.query(DatasetVersion)
        .filter(DatasetVersion.dataset_id == dataset_id)
        .order_by(DatasetVersion.created_at.desc())
        .all()
    )
    return {
        "dataset_id": dataset_id,
        "versions": [
            {
                "id": v.id,
                "version": v.version,
                "manifest_key": v.manifest_key,
                "meta_key": v.meta_key,
                "created_at": v.created_at.isoformat() + "Z",
            }
            for v in versions
        ],
    }


@app.post("/datasets/{dataset_id}/publish")
def publish_dataset(dataset_id: int, payload: PublishVersionRequest, db: Session = Depends(get_db)) -> dict:
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="dataset not found")

    job = Job(
        kind="publish_version",
        status=JobStatus.queued,
        payload_json=json.dumps(payload.model_dump()),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    task = publish_dataset_version.delay(job.id, dataset_id, payload.version, payload.include_object_ids)
    job.celery_task_id = task.id
    db.commit()

    return {"job_id": job.id, "task_id": task.id, "status": job.status}


@app.post("/datasets/{dataset_id}/ingest/recipe1m")
def ingest_recipe1m(
    dataset_id: int,
    payload: Recipe1MSampleIngestRequest,
    db: Session = Depends(get_db),
) -> dict:
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="dataset not found")

    job = Job(
        kind="recipe1m_ingest",
        status=JobStatus.queued,
        payload_json=json.dumps(payload.model_dump()),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    task = ingest_recipe1m_sample.delay(
        job.id,
        dataset_id,
        payload.manifest_source,
        payload.sample_size,
        payload.raw_prefix,
        payload.target_container,
        payload.auto_publish_version,
    )
    job.celery_task_id = task.id
    db.commit()

    return {"job_id": job.id, "task_id": task.id, "status": job.status}


@app.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "message": job.message,
        "payload": json.loads(job.payload_json),
        "created_at": job.created_at.isoformat() + "Z",
        "updated_at": job.updated_at.isoformat() + "Z",
    }
