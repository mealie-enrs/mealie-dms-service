from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Enum as SQLEnum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from dms.db import Base


class UploadStatus(str, Enum):
    incoming = "incoming"
    approved = "approved"
    rejected = "rejected"
    quarantined = "quarantined"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class ObjectSource(str, Enum):
    """Where the object came from — used to filter training data."""
    user_upload = "user_upload"   # real user submitted via the upload API
    kaggle = "kaggle"             # downloaded from Kaggle via download_kaggle_dataset
    recipe1m = "recipe1m"        # ingested from Recipe1M public dataset
    synthetic = "synthetic"      # generated / augmented by the pipeline


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    original_filename: Mapped[str] = mapped_column(String(512))
    status: Mapped[UploadStatus] = mapped_column(SQLEnum(UploadStatus), default=UploadStatus.incoming)
    incoming_key: Mapped[str] = mapped_column(String(1024), unique=True)
    curated_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Governance fields
    country: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_test_account: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Object(Base):
    __tablename__ = "objects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    checksum_sha256: Mapped[str] = mapped_column(String(128), index=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_upload_id: Mapped[int | None] = mapped_column(ForeignKey("uploads.id"), nullable=True)

    # Governance fields
    source: Mapped[ObjectSource] = mapped_column(
        SQLEnum(ObjectSource), default=ObjectSource.user_upload, nullable=False, index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    is_test_account: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)
    version: Mapped[str] = mapped_column(String(64), index=True)
    manifest_key: Mapped[str] = mapped_column(String(1024))
    meta_key: Mapped[str] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DatasetItem(Base):
    __tablename__ = "dataset_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), index=True)
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    split: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[JobStatus] = mapped_column(SQLEnum(JobStatus), default=JobStatus.queued, index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
