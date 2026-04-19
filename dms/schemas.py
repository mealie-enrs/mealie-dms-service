from __future__ import annotations

from pydantic import BaseModel, Field


class UploadInitRequest(BaseModel):
    user_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)


class UploadInitResponse(BaseModel):
    upload_id: int
    incoming_key: str


class ApproveUploadRequest(BaseModel):
    approve: bool = True


class DatasetCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None


class PublishVersionRequest(BaseModel):
    version: str = Field(min_length=1)
    include_object_ids: list[int]


class Recipe1MSampleIngestRequest(BaseModel):
    manifest_source: str = Field(min_length=1, description="Local path or URL to Recipe1M-style JSONL")
    sample_size: int = Field(default=1000, ge=1, le=50000)
    raw_prefix: str = Field(default="raw/recipe1m")
    target_container: str | None = None
    auto_publish_version: str | None = None


class CompileTrainingDatasetRequest(BaseModel):
    """Compile a versioned train/val/test dataset from the Kaggle food-images data."""

    version: str = Field(default="v1", min_length=1, description="Version tag, e.g. v1, v2.")
    container: str | None = Field(
        default=None,
        description="Swift container (defaults to SWIFT_TRAINING_CONTAINER).",
    )
    enable_augmentation: bool = Field(
        default=False,
        description="Generate synthetic augmented variants via Albumentations. "
                    "Slow (requires 4 Swift round-trips per training image). "
                    "Disable for fast index builds; enable only for final training runs.",
    )


class CompileRecipeNLGDatasetRequest(BaseModel):
    version: str = Field(default="v1", min_length=1, description="Version tag, e.g. v1.")
    container: str | None = Field(
        default=None,
        description="Swift container (defaults to SWIFT_TRAINING_CONTAINER).",
    )
    source_key: str = Field(
        default="recipenlg/RecipeNLG_dataset.csv",
        min_length=1,
        description="Swift object key for the raw RecipeNLG CSV.",
    )


class TrainingPipelineRequest(BaseModel):
    """Trigger the end-to-end Prefect training pipeline."""

    version: str = Field(default="v1", min_length=1, description="Dataset version tag, e.g. v1, v2.")
    dataset_id: int = Field(default=1, ge=1, description="ID of the Dataset row to register the version under.")
    skip_download: bool = Field(default=True, description="Skip the Kaggle download step (use existing Swift data).")
    enable_augmentation: bool = Field(
        default=False,
        description="Generate synthetic augmented variants via Albumentations. "
                    "Very slow (4 Swift round-trips per training image). "
                    "Leave False for fast index builds.",
    )


class DraftRequest(BaseModel):
    """Request a recipe draft generated from a food image."""

    image_b64: str | None = Field(
        default=None,
        description="Base64-encoded image (JPEG/PNG). Either this or swift_key is required.",
    )
    swift_key: str | None = Field(
        default=None,
        description="Object key in the Swift training container, e.g. "
                    "'recipe1m/kaggle-food-images/Food Images/Food Images/0.jpg'. "
                    "Used when the image is already stored in Swift.",
    )
    top_k: int = Field(
        default=5, ge=1, le=20,
        description="Number of nearest-neighbour recipes to retrieve from Qdrant.",
    )


class ImageValidationSchema(BaseModel):
    is_valid: bool
    blur_score: float
    food_confidence: float
    rejection_reason: str | None = None
    width: int = 0
    height: int = 0


class DraftResponse(BaseModel):
    """Structured recipe draft returned by POST /inference/draft."""

    draft_id: str = Field(description="UUID — include in feedback payload to link draft → final recipe.")
    validation: ImageValidationSchema
    title: str
    ingredients: list[str]
    steps: list[str]
    tags: list[str]
    confidence: float = Field(description="Overall draft quality signal, 0.0–1.0.")
    disclaimer: str
    top_k_matches: list[dict] = Field(description="Raw Qdrant nearest-neighbour results.")


class KaggleDatasetDownloadRequest(BaseModel):
    """Download a Kaggle dataset on the worker and optionally upload to Swift."""

    dataset_slug: str = Field(
        default="pes12017000148/food-ingredients-and-recipe-dataset-with-images",
        min_length=3,
        description="Kaggle dataset slug (<owner>/<dataset>).",
    )
    upload_to_swift: bool = Field(
        default=True,
        description="Upload downloaded files to Swift.",
    )
    swift_prefix: str | None = Field(
        default=None,
        description="Top-level Swift prefix for this dataset, e.g. 'recipe1m' or 'recipenlg'.",
    )
    swift_subpath: str = Field(
        default="",
        description="Optional sub-path under the prefix, e.g. 'kaggle/food-images' -> recipenlg/kaggle/food-images/...",
    )
