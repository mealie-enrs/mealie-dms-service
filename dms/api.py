import logging
import json
import uuid
from datetime import datetime

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from dms.config import settings
from dms.db import Base, engine, get_db
from dms import inference
from dms.models import Dataset, DatasetVersion, Job, JobStatus, Upload, UploadStatus
from dms.schemas import (
    ApproveUploadRequest,
    CompileRecipeNLGDatasetRequest,
    CompileTrainingDatasetRequest,
    DatasetCreateRequest,
    KaggleDatasetDownloadRequest,
    PublishVersionRequest,
    Recipe1MSampleIngestRequest,
    TrainingPipelineRequest,
    UploadInitRequest,
    UploadInitResponse,
)
from dms import events
from dms.tasks import (
    compile_recipenlg_dataset,
    compile_training_dataset,
    download_kaggle_dataset,
    ingest_recipe1m_sample,
    process_upload_approval,
    publish_dataset_version,
    score_upload_risk,
)

app = FastAPI(title="DMS API")
logger = logging.getLogger(__name__)

# Expose /metrics for Prometheus scraping
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)

    # Ensure Qdrant collection exists (non-fatal if Qdrant is not ready yet)
    try:
        inference.ensure_collection()
        logger.info("Qdrant collection '%s' ready.", settings.qdrant_collection)
    except Exception:
        logger.warning("Qdrant not reachable on startup — will retry on first request.")

    # Preload ResNet50 weights (non-fatal — retries on first /inference/features call)
    try:
        inference.load_model()
        logger.info("Preloaded inference model '%s'.", settings.inference_model_name)
    except Exception:
        logger.warning("Inference model preload failed — will retry on first request.")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/inference/index")
def inference_index() -> dict:
    """Return Qdrant collection stats — how many recipes are indexed."""
    try:
        return inference.get_collection_info()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/inference/index/build")
def build_inference_index(db: Session = Depends(get_db)) -> dict:
    """
    Kick off a background job that reads the latest compiled manifest from
    Swift, embeds every image through ResNet50, and upserts into Qdrant.
    """
    from dms.tasks import build_qdrant_index

    job = Job(
        kind="build_qdrant_index",
        status=JobStatus.queued,
        payload_json=json.dumps({"manifest_key": settings.inference_manifest_key}),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    task = build_qdrant_index.delay(job.id, settings.inference_manifest_key)
    job.celery_task_id = task.id
    db.commit()

    return {"job_id": job.id, "task_id": task.id, "status": job.status}


@app.post("/inference/features")
async def inference_features(
    request: Request,
    file: UploadFile | None = File(default=None),
    image_base64: str | None = Form(default=None),
    top_k: int | None = Form(default=None),
) -> dict:
    top_k_value = top_k if top_k is not None else settings.inference_top_k_default

    if request.headers.get("content-type", "").startswith("application/json"):
        payload = await request.json()
        image_base64 = payload.get("image_base64")
        top_k_value = int(payload.get("top_k", top_k_value))

    if top_k_value < 1 or top_k_value > settings.inference_top_k_max:
        raise HTTPException(
            status_code=422,
            detail=f"top_k must be between 1 and {settings.inference_top_k_max}",
        )

    try:
        file_bytes = await file.read() if file is not None else None
        raw_bytes = inference.decode_image_bytes(file_bytes=file_bytes, image_base64=image_base64)
        embedding = inference.compute_embedding(raw_bytes)
        matches = inference.search_qdrant(embedding, top_k_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    events.emit_inference_request(None, top_k_value, len(matches))
    return {
        "model": settings.inference_model_name,
        "embedding_dim": int(embedding.shape[0]),
        "embedding": embedding.astype(float).tolist(),
        "top_k": top_k_value,
        "matches": matches,
    }


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

    events.emit_upload_created(upload.id, payload.user_id, payload.filename)
    score_upload_risk.delay(upload.id)

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

    events.emit_upload_approved(upload_id, upload.user_id, payload.approve)
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


@app.post("/datasets/{dataset_id}/compile")
def compile_dataset(
    dataset_id: int,
    payload: CompileTrainingDatasetRequest,
    db: Session = Depends(get_db),
) -> dict:
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="dataset not found")

    job = Job(
        kind="compile_training_dataset",
        status=JobStatus.queued,
        payload_json=json.dumps(payload.model_dump()),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    task = compile_training_dataset.delay(
        job.id, dataset_id, payload.version, payload.container,
        payload.enable_augmentation,
    )
    job.celery_task_id = task.id
    db.commit()

    return {"job_id": job.id, "task_id": task.id, "status": job.status}


@app.post("/datasets/{dataset_id}/compile/recipenlg")
def compile_recipenlg(
    dataset_id: int,
    payload: CompileRecipeNLGDatasetRequest,
    db: Session = Depends(get_db),
) -> dict:
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="dataset not found")

    job = Job(
        kind="compile_recipenlg_dataset",
        status=JobStatus.queued,
        payload_json=json.dumps(payload.model_dump()),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    task = compile_recipenlg_dataset.delay(
        job.id, dataset_id, payload.version, payload.container, payload.source_key
    )
    job.celery_task_id = task.id
    db.commit()

    return {"job_id": job.id, "task_id": task.id, "status": job.status}


@app.post("/kaggle/download")
def kaggle_download(payload: KaggleDatasetDownloadRequest, db: Session = Depends(get_db)) -> dict:
    job = Job(
        kind="kaggle_dataset_download",
        status=JobStatus.queued,
        payload_json=json.dumps(payload.model_dump()),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    task = download_kaggle_dataset.delay(
        job.id,
        payload.dataset_slug,
        payload.upload_to_swift,
        payload.swift_prefix,
        payload.swift_subpath,
    )
    job.celery_task_id = task.id
    db.commit()

    return {"job_id": job.id, "task_id": task.id, "status": job.status}


@app.post("/pipelines/training")
def trigger_training_pipeline(payload: TrainingPipelineRequest) -> dict:
    """
    Trigger the full Prefect batch pipeline asynchronously via Celery.
    Runs: compile → quality check → augmentation → manifest → Qdrant index build.
    """
    from dms.tasks import run_training_pipeline
    task = run_training_pipeline.delay(
        payload.version,
        payload.dataset_id,
        payload.skip_download,
        payload.enable_augmentation,
    )
    return {"task_id": task.id, "version": payload.version, "status": "queued"}


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
