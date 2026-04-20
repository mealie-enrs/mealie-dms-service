import json
import logging
import uuid
from datetime import datetime
from difflib import SequenceMatcher

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import Float, func
from sqlalchemy.orm import Session

from dms import events
from dms import inference
from dms.config import settings
from dms.db import Base, SessionLocal, engine, get_db
from dms.models import (
    Dataset,
    DatasetVersion,
    DraftCapture,
    Feedback,
    FeedbackAction,
    Job,
    JobStatus,
    Upload,
    UploadStatus,
)
from dms.schemas import (
    ApproveUploadRequest,
    CompileRecipeNLGDatasetRequest,
    CompileTrainingDatasetRequest,
    DatasetCreateRequest,
    DraftRequest,
    DraftResponse,
    FeedbackMetricsResponse,
    FeedbackRequest,
    FeedbackResponse,
    KaggleDatasetDownloadRequest,
    PublishVersionRequest,
    Recipe1MSampleIngestRequest,
    TrainingPipelineRequest,
    UploadInitRequest,
    UploadInitResponse,
)
from dms.tasks import (
    compile_recipenlg_dataset,
    compile_training_dataset,
    download_kaggle_dataset,
    ingest_recipe1m_sample,
    process_upload_approval,
    publish_dataset_version,
    score_upload_risk,
)
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

app = FastAPI(title="DMS API")
logger = logging.getLogger(__name__)

# Expose /metrics for Prometheus scraping
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)


def _isoformat_utc(value: datetime) -> str:
    return value.isoformat() + "Z"


def _serialise_draft_response(response: DraftResponse, image_key: str | None) -> dict:
    return {
        "draft_id": response.draft_id,
        "image_key": image_key,
        "validation": response.validation.model_dump(),
        "title": response.title,
        "ingredients": response.ingredients,
        "steps": response.steps,
        "tags": response.tags,
        "confidence": response.confidence,
        "disclaimer": response.disclaimer,
        "top_k_matches": response.top_k_matches,
    }


def _final_recipe_payload(payload: FeedbackRequest) -> dict | None:
    if payload.action == FeedbackAction.rejected.value:
        return None
    return {
        "title": payload.final_title,
        "ingredients": payload.final_ingredients,
        "steps": payload.final_steps,
    }


def _compute_edit_distance(draft_shown: dict, payload: FeedbackRequest) -> float | None:
    if payload.action == FeedbackAction.rejected.value:
        return None

    before = json.dumps(
        {
            "title": draft_shown.get("title", ""),
            "ingredients": draft_shown.get("ingredients", []),
            "steps": draft_shown.get("steps", []),
        },
        sort_keys=True,
    )
    after = json.dumps(_final_recipe_payload(payload), sort_keys=True)
    similarity = SequenceMatcher(a=before, b=after).ratio()
    return round(1.0 - similarity, 4)


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _feedback_metrics_snapshot(db: Session) -> dict[str, float | int | None]:
    rows = db.query(Feedback.action, func.count(Feedback.id)).group_by(Feedback.action).all()
    counts = {action.value if hasattr(action, "value") else str(action): count for action, count in rows}

    total = sum(counts.values())
    approved = counts.get(FeedbackAction.approved.value, 0)
    edited = counts.get(FeedbackAction.edited.value, 0)
    rejected = counts.get(FeedbackAction.rejected.value, 0)
    avg_consent_rate = db.query(func.avg(func.cast(Feedback.consent, Float))).scalar()
    avg_edit_distance = db.query(func.avg(Feedback.edit_distance)).scalar()

    return {
        "total": total,
        "approved": approved,
        "edited": edited,
        "rejected": rejected,
        "approval_rate": _safe_rate(approved, total),
        "avg_consent_rate": round(float(avg_consent_rate or 0.0), 4),
        "avg_edit_distance": None if avg_edit_distance is None else round(float(avg_edit_distance), 4),
    }


def _build_business_metrics(db: Session) -> bytes:
    registry = CollectorRegistry()

    feedback_total = Gauge("dms_feedback_total", "Total feedback records stored in DMS", registry=registry)
    feedback_action_total = Gauge(
        "dms_feedback_action_total",
        "Feedback records by action",
        ["action"],
        registry=registry,
    )
    feedback_approval_rate = Gauge(
        "dms_feedback_approval_rate",
        "Approved feedback ratio",
        registry=registry,
    )
    feedback_consent_rate = Gauge(
        "dms_feedback_consent_rate",
        "Average consent ratio over feedback records",
        registry=registry,
    )
    feedback_avg_edit_distance = Gauge(
        "dms_feedback_avg_edit_distance",
        "Average edit distance for approved or edited recipes",
        registry=registry,
    )
    upload_total = Gauge(
        "dms_upload_total",
        "Uploads by moderation status",
        ["status"],
        registry=registry,
    )
    job_total = Gauge(
        "dms_jobs_total",
        "Jobs by kind and status",
        ["kind", "status"],
        registry=registry,
    )
    draft_capture_total = Gauge(
        "dms_draft_capture_total",
        "Draft captures stored for feedback closure",
        registry=registry,
    )

    feedback = _feedback_metrics_snapshot(db)
    feedback_total.set(int(feedback["total"]))
    feedback_action_total.labels("approved").set(int(feedback["approved"]))
    feedback_action_total.labels("edited").set(int(feedback["edited"]))
    feedback_action_total.labels("rejected").set(int(feedback["rejected"]))
    feedback_approval_rate.set(float(feedback["approval_rate"]))
    feedback_consent_rate.set(float(feedback["avg_consent_rate"]))
    feedback_avg_edit_distance.set(float(feedback["avg_edit_distance"] or 0.0))

    upload_rows = db.query(Upload.status, func.count(Upload.id)).group_by(Upload.status).all()
    for status, count in upload_rows:
        upload_total.labels(status.value if hasattr(status, "value") else str(status)).set(int(count))

    job_rows = db.query(Job.kind, Job.status, func.count(Job.id)).group_by(Job.kind, Job.status).all()
    for kind, status, count in job_rows:
        job_total.labels(
            kind,
            status.value if hasattr(status, "value") else str(status),
        ).set(int(count))

    draft_capture_total.set(int(db.query(func.count(DraftCapture.draft_id)).scalar() or 0))
    return generate_latest(registry)


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


@app.post("/inference/draft", response_model=DraftResponse)
async def inference_draft(payload: DraftRequest, db: Session = Depends(get_db)) -> DraftResponse:
    """
    Generate a structured recipe draft from a food image.

    Accepts either a base64-encoded image (image_b64) or a Swift object key
    (swift_key). Returns a complete recipe draft with title, ingredients,
    steps, tags, confidence score, and a draft_id for downstream feedback capture.

    The draft is template-based (fast, no LLM required). The draft_id should
    be stored by the caller and included in the POST /feedback payload when the
    user approves/edits the recipe, enabling the retraining feedback loop.
    """
    from dms import draft as draft_module

    raw_bytes: bytes | None = None
    try:
        if payload.image_b64:
            raw_bytes = inference.decode_image_bytes(image_base64=payload.image_b64)
        elif payload.swift_key:
            from dms.storage import require_swift

            conn = require_swift()
            _, data = conn.get_object(settings.swift_training_container, payload.swift_key)
            raw_bytes = bytes(data)
        else:
            raise HTTPException(
                status_code=422,
                detail="Provide either image_b64 (base64 string) or swift_key.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not load image: {exc}") from exc

    try:
        embedding = inference.compute_embedding(raw_bytes)
        matches = inference.search_qdrant(embedding, payload.top_k)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Embedding/retrieval failed: {exc}") from exc

    result = draft_module.generate_draft(raw_bytes, matches, swift_key=payload.swift_key)

    response = DraftResponse(
        draft_id=result.draft_id,
        validation=result.validation.__dict__,
        title=result.title,
        ingredients=result.ingredients,
        steps=result.steps,
        tags=result.tags,
        confidence=result.confidence,
        disclaimer=result.disclaimer,
        top_k_matches=result.top_k_matches,
    )

    draft_capture = DraftCapture(
        draft_id=response.draft_id,
        image_key=payload.swift_key,
        draft_shown=_serialise_draft_response(response, payload.swift_key),
    )
    db.merge(draft_capture)
    db.commit()

    events.emit_inference_request(None, payload.top_k, len(matches))
    return response


@app.post("/feedback", response_model=FeedbackResponse, status_code=201)
def create_feedback(payload: FeedbackRequest, db: Session = Depends(get_db)) -> FeedbackResponse:
    draft_capture = db.get(DraftCapture, payload.draft_id)
    if not draft_capture:
        raise HTTPException(status_code=404, detail="draft not found")

    edit_distance = _compute_edit_distance(draft_capture.draft_shown, payload)
    feedback = Feedback(
        draft_id=payload.draft_id,
        image_key=draft_capture.image_key,
        draft_shown=draft_capture.draft_shown,
        final_saved=_final_recipe_payload(payload),
        edit_distance=edit_distance,
        action=FeedbackAction(payload.action),
        consent=payload.consent,
        mealie_recipe_slug=payload.mealie_recipe_slug,
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)

    events.emit_feedback_captured(
        feedback_id=feedback.id,
        draft_id=feedback.draft_id,
        action=feedback.action.value,
        consent=feedback.consent,
        edit_distance=feedback.edit_distance,
        image_key=feedback.image_key,
        mealie_recipe_slug=feedback.mealie_recipe_slug,
    )

    return FeedbackResponse(
        status="ok",
        draft_id=feedback.draft_id,
        feedback_id=feedback.id,
        edit_distance=feedback.edit_distance,
    )


@app.get("/metrics/feedback", response_model=FeedbackMetricsResponse)
def get_feedback_metrics(db: Session = Depends(get_db)) -> FeedbackMetricsResponse:
    feedback = _feedback_metrics_snapshot(db)
    return FeedbackMetricsResponse(
        total=int(feedback["total"]),
        approved=int(feedback["approved"]),
        edited=int(feedback["edited"]),
        rejected=int(feedback["rejected"]),
        approval_rate=float(feedback["approval_rate"]),
        avg_consent_rate=float(feedback["avg_consent_rate"]),
        avg_edit_distance=feedback["avg_edit_distance"],
    )


@app.get("/metrics/business")
def get_business_metrics() -> Response:
    db = SessionLocal()
    try:
        payload = _build_business_metrics(db)
    finally:
        db.close()
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


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
                "created_at": _isoformat_utc(v.created_at),
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
        "created_at": _isoformat_utc(job.created_at),
        "updated_at": _isoformat_utc(job.updated_at),
    }
