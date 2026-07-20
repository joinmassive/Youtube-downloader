from __future__ import annotations

from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, Field, HttpUrl, model_validator


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


class Delivery(BaseModel):
    type: Literal["local", "s3"]
    bucket: str | None = None
    key_prefix: str = ""
    region: str | None = None

    @model_validator(mode="after")
    def requires_bucket_for_s3(self) -> "Delivery":
        if self.type == "s3" and not self.bucket:
            raise ValueError("S3 delivery requires bucket")
        return self


class SubmitJob(BaseModel):
    url: HttpUrl
    profile: str = "best_progressive"
    delivery: Delivery
    webhook_url: HttpUrl | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class Job(BaseModel):
    id: str
    status: JobStatus
    request: SubmitJob
    attempts: int = 0
    error_code: str | None = None
    error_message: str | None = None
    result: dict[str, object] | None = None
