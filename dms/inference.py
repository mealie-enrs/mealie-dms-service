"""
Inference module — online feature computation path.

Architecture:
  1. ResNet50 (torchvision) computes a 2048-D L2-normalised embedding from an image.
  2. Qdrant (vector DB) stores all recipe embeddings + metadata.
  3. /inference/features  → embed query image → Qdrant cosine search → top-K matches.
  4. /inference/index/build → Celery task loads manifest from Swift, embeds every
     image, upserts into Qdrant.  Replaces the old NPZ file approach.
"""
from __future__ import annotations

import base64
import importlib
import io
import logging
import uuid
from dataclasses import dataclass

import numpy as np
from PIL import Image

from dms.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class InferenceModel:
    name: str
    embedding_dim: int
    extractor: object
    preprocess: object


_MODEL_CACHE: InferenceModel | None = None


def load_model() -> InferenceModel:
    global _MODEL_CACHE
    if _MODEL_CACHE is not None:
        return _MODEL_CACHE

    try:
        torch = importlib.import_module("torch")
        models = importlib.import_module("torchvision.models")
    except ImportError as exc:
        raise RuntimeError("Install torch and torchvision for inference.") from exc

    weights = models.ResNet50_Weights.DEFAULT
    backbone = models.resnet50(weights=weights)
    extractor = torch.nn.Sequential(*list(backbone.children())[:-1])
    extractor.eval()

    _MODEL_CACHE = InferenceModel(
        name=settings.inference_model_name,
        embedding_dim=2048,
        extractor=extractor,
        preprocess=weights.transforms(),
    )
    log.info("ResNet50 loaded (embedding_dim=2048)")
    return _MODEL_CACHE


def compute_embedding(raw_bytes: bytes) -> np.ndarray:
    model = load_model()
    torch = importlib.import_module("torch")

    image = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    tensor = model.preprocess(image).unsqueeze(0)
    with torch.no_grad():
        features = model.extractor(tensor).flatten(start_dim=1).cpu().numpy()[0].astype(np.float32)

    norm = np.linalg.norm(features)
    if norm > 0:
        features = features / norm
    return features


def decode_image_bytes(*, file_bytes: bytes | None = None, image_base64: str | None = None) -> bytes:
    if file_bytes:
        return file_bytes
    if image_base64:
        payload = image_base64.strip()
        if payload.startswith("data:") and "," in payload:
            payload = payload.split(",", 1)[1]
        return base64.b64decode(payload)
    raise ValueError("Provide either file bytes or image_base64.")


# ---------------------------------------------------------------------------
# Qdrant vector store
# ---------------------------------------------------------------------------

def _get_qdrant_client():
    from qdrant_client import QdrantClient
    return QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)


def ensure_collection() -> None:
    from qdrant_client.models import Distance, VectorParams
    client = _get_qdrant_client()
    existing = [c.name for c in client.get_collections().collections]
    if settings.qdrant_collection not in existing:
        client.create_collection(
            settings.qdrant_collection,
            vectors_config=VectorParams(size=2048, distance=Distance.COSINE),
        )
        log.info("Created Qdrant collection '%s'", settings.qdrant_collection)


def search_qdrant(embedding: np.ndarray, top_k: int) -> list[dict]:
    client = _get_qdrant_client()
    results = client.search(
        collection_name=settings.qdrant_collection,
        query_vector=embedding.tolist(),
        limit=top_k,
        with_payload=True,
    )
    return [
        {
            "object_key": r.payload.get("object_key", ""),
            "title": r.payload.get("title", ""),
            "ingredients": r.payload.get("ingredients", ""),
            "split": r.payload.get("split", ""),
            "score": float(r.score),
        }
        for r in results
    ]


def get_collection_info() -> dict:
    client = _get_qdrant_client()
    existing = [c.name for c in client.get_collections().collections]
    if settings.qdrant_collection not in existing:
        return {"collection": settings.qdrant_collection, "vectors_count": 0, "status": "empty"}
    info = client.get_collection(settings.qdrant_collection)
    return {
        "collection": settings.qdrant_collection,
        "vectors_count": info.vectors_count,
        "indexed_vectors_count": info.indexed_vectors_count,
        "status": str(info.status),
    }


def upsert_batch(points: list[dict]) -> int:
    """Upsert a batch of {id, vector, payload} dicts into Qdrant."""
    from qdrant_client.models import PointStruct
    ensure_collection()
    client = _get_qdrant_client()

    qdrant_points = [
        PointStruct(
            id=p.get("id", str(uuid.uuid4())),
            vector=p["vector"],
            payload=p.get("payload", {}),
        )
        for p in points
    ]

    batch_size = 128
    inserted = 0
    for i in range(0, len(qdrant_points), batch_size):
        client.upsert(collection_name=settings.qdrant_collection, points=qdrant_points[i:i + batch_size])
        inserted += len(qdrant_points[i:i + batch_size])

    return inserted
