"""File-based project storage: one folder per project under `data_dir/projects`,
metadata in `metadata.yaml`. No database -- an in-memory cache (built once at
startup by scanning disk) backs fast reads; every mutation writes through to
disk (atomic tmp-file + rename) before updating the cache.

Live processing status/progress is intentionally *not* part of this cache --
see greenplan.api.jobs.read_job_record, which always reads job.yaml fresh.
Keeping the two concerns in separate files means a job running in a worker
subprocess never needs to reach back into this process's cache to stay
consistent.
"""

from __future__ import annotations

import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel

from greenplan.api._yamlio import atomic_write_yaml

PROJECTS_DIRNAME = "projects"
METADATA_FILENAME = "metadata.yaml"


class ProjectNotFoundError(Exception):
    pass


class ProjectRecord(BaseModel):
    id: str
    name: str
    description: str | None = None
    # (minx, miny, maxx, maxy), WGS84 lon/lat -- required at the API request
    # schema level (ProjectCreateRequest), but stays optional here so that
    # project folders written before this field existed don't fail
    # validation on startup (ProjectStore.load() reads every metadata.yaml).
    bbox_user: tuple[float, float, float, float] | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProjectStore:
    """In-memory cache of ProjectRecord, backed by one metadata.yaml per
    project folder. Not safe for use from multiple processes (only the API
    process itself ever writes metadata.yaml); worker subprocesses only ever
    touch job.yaml and the project's raw/processed files.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.projects_dir = data_dir / PROJECTS_DIRNAME
        self._cache: dict[str, ProjectRecord] = {}
        self._lock = threading.Lock()

    def load(self) -> None:
        """Scan disk once (startup) and populate the in-memory cache."""
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        cache: dict[str, ProjectRecord] = {}
        for project_dir in self.projects_dir.iterdir():
            metadata_path = project_dir / METADATA_FILENAME
            if not project_dir.is_dir() or not metadata_path.is_file():
                continue
            record = ProjectRecord.model_validate(
                yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
            )
            cache[record.id] = record
        with self._lock:
            self._cache = cache

    def project_dir(self, project_id: str) -> Path:
        return self.projects_dir / project_id

    def list_active(self) -> list[ProjectRecord]:
        with self._lock:
            records = list(self._cache.values())
        return sorted((r for r in records if not r.is_deleted), key=lambda r: r.created_at)

    def get(self, project_id: str) -> ProjectRecord | None:
        with self._lock:
            record = self._cache.get(project_id)
        if record is None or record.is_deleted:
            return None
        return record

    def create(
        self,
        name: str,
        description: str | None,
        bbox_user: tuple[float, float, float, float] | None = None,
    ) -> ProjectRecord:
        project_id = uuid.uuid4().hex
        now = _now()
        record = ProjectRecord(
            id=project_id, name=name, description=description, bbox_user=bbox_user,
            created_at=now, updated_at=now,
        )
        project_dir = self.project_dir(project_id)
        project_dir.mkdir(parents=True)
        (project_dir / "raw").mkdir()
        (project_dir / "processed").mkdir()
        self._persist(record)
        return record

    def update(self, project_id: str, name: str | None, description: str | None) -> ProjectRecord:
        with self._lock:
            record = self._cache.get(project_id)
            if record is None or record.is_deleted:
                raise ProjectNotFoundError(project_id)
            updated = record.model_copy(
                update={
                    "name": name if name is not None else record.name,
                    "description": description if description is not None else record.description,
                    "updated_at": _now(),
                }
            )
        self._persist(updated)
        return updated

    def soft_delete(self, project_id: str) -> None:
        with self._lock:
            record = self._cache.get(project_id)
            if record is None or record.is_deleted:
                raise ProjectNotFoundError(project_id)
            updated = record.model_copy(update={"deleted_at": _now(), "updated_at": _now()})
        self._persist(updated)

    def _persist(self, record: ProjectRecord) -> None:
        metadata_path = self.project_dir(record.id) / METADATA_FILENAME
        atomic_write_yaml(metadata_path, record.model_dump(mode="json"))
        with self._lock:
            self._cache[record.id] = record

    def purge_all(self) -> None:
        """Test helper: wipe every project from disk and the cache."""
        if self.projects_dir.exists():
            shutil.rmtree(self.projects_dir)
        self.projects_dir.mkdir(parents=True)
        with self._lock:
            self._cache = {}
