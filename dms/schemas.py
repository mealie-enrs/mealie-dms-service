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
