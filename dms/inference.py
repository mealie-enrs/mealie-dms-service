from __future__ import annotations

import base64
import importlib
import io
import json
from dataclasses import dataclass

import numpy as np
from PIL import Image

from dms.config import settings
from dms.storage import get_bytes, put_bytes


@dataclass
class FeatureIndex:
    embeddings: np.ndarray
    object_keys: list[str]
    titles: list[str]
    ingredients: list[str]
    splits: list[str]
    source_manifest_key: str


@dataclass
class InferenceModel:
    name: str
    embedding_dim: int
    extractor: object
    preprocess: object


_MODEL_CACHE: InferenceModel | None = None
_INDEX_CACHE: FeatureIndex | None = None


def decode_image_bytes(*, file_bytes: bytes | None = None, image_base64: str | None = None) -> bytes:
    if file_bytes:
        return file_bytes
    if image_base64:
        payload = image_base64.strip()
        if payload.startswith("data:") and "," in payload:
            payload = payload.split(",", 1)[1]
        return base64.b64decode(payload)
    raise ValueError("Provide either file bytes or image_base64.")


def load_model() -> InferenceModel:
    global _MODEL_CACHE
    if _MODEL_CACHE is not None:
        return _MODEL_CACHE

    try:
        torch = importlib.import_module("torch")
        models = importlib.import_module("torchvision.models")
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Inference dependencies are missing. Install torch and torchvision.") from exc

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


def cosine_top_k(embedding: np.ndarray, index: FeatureIndex, top_k: int) -> list[dict]:
    if index.embeddings.size == 0:
        return []

    scores = index.embeddings @ embedding
    k = min(top_k, len(index.object_keys))
    top_idx = np.argsort(scores)[-k:][::-1]
    return [
        {
            "object_key": index.object_keys[idx],
            "title": index.titles[idx],
            "ingredients": index.ingredients[idx],
            "split": index.splits[idx],
            "score": float(scores[idx]),
        }
        for idx in top_idx
    ]


def load_index(force_reload: bool = False) -> FeatureIndex:
    global _INDEX_CACHE
    if _INDEX_CACHE is not None and not force_reload:
        return _INDEX_CACHE

    raw = get_bytes(settings.swift_training_container, settings.inference_index_key)
    with np.load(io.BytesIO(raw), allow_pickle=False) as data:
        _INDEX_CACHE = FeatureIndex(
            embeddings=data["embeddings"].astype(np.float32),
            object_keys=data["object_keys"].astype(str).tolist(),
            titles=data["titles"].astype(str).tolist(),
            ingredients=data["ingredients"].astype(str).tolist(),
            splits=data["splits"].astype(str).tolist(),
            source_manifest_key=str(data["source_manifest_key"][0]),
        )
    return _INDEX_CACHE


def load_index_meta() -> dict:
    raw = get_bytes(settings.swift_training_container, settings.inference_index_meta_key)
    return json.loads(raw.decode("utf-8"))


def save_index_bytes(content: bytes) -> None:
    put_bytes(settings.swift_training_container, settings.inference_index_key, content)


def save_index_meta(meta: dict) -> None:
    put_bytes(
        settings.swift_training_container,
        settings.inference_index_meta_key,
        json.dumps(meta, indent=2).encode("utf-8"),
        "application/json",
    )
