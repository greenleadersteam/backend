from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from greenplan.api.jobs import GeoreferenceInfo, JobError, JobRecord


class ProjectCreateRequest(BaseModel):
    name: str
    description: str | None = None
    # (minx, miny, maxx, maxy), WGS84 lon/lat: an approximate bbox of the
    # project site, required to disambiguate geobridge.ru geodetic-point
    # matches during the mandatory georeferencing stage -- see
    # greenplan.georeference.transform's module docstring for why this can't
    # be skipped (catalog point numbers are not globally unique).
    bbox_user: tuple[float, float, float, float]


class ProjectUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class JobStatus(BaseModel):
    stage: str
    progress_pct: int
    error: JobError | None = None
    georeference: GeoreferenceInfo | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_record(cls, record: JobRecord | None) -> "JobStatus":
        if record is None:
            return cls(stage="draft", progress_pct=0)
        return cls(**record.model_dump())


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    status: str
    job: JobStatus
