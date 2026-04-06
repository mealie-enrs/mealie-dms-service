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


class KaggleDatasetDownloadRequest(BaseModel):
    """Download a Kaggle dataset on the worker and optionally upload to Swift."""

    dataset_slug: str = Field(
        default="pes12017000148/food-ingredients-and-recipe-dataset-with-images",
        min_length=3,
        description="Kaggle dataset slug (<owner>/<dataset>).",
    )
    upload_to_swift: bool = Field(
        default=True,
        description="Upload downloaded files to Swift under SWIFT_RECIPE1M_PREFIX.",
    )
    swift_subpath: str = Field(
        default="",
        description="Optional sub-path under the prefix, e.g. 'kaggle/food-images' -> recipe1m/kaggle/food-images/...",
    )
