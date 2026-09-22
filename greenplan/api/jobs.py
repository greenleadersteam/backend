"""Background processing: extract the uploaded zip, run the existing
greenplan pipeline (parse_folder -> plant_folder -> DXF/GeoJSON export), and
track progress in job.yaml so the API can report it without any IPC beyond
the filesystem.

`run_processing_job` is the unit of work submitted to a
concurrent.futures.Executor (a ProcessPoolExecutor in production, so a stuck
or crashing DXF takes down its own worker process rather than the API; tests
can inject a ThreadPoolExecutor instead since the function itself doesn't
care which kind of executor runs it). It must stay a plain, top-level,
picklable function -- no closures, no bound methods -- to survive being sent
to a worker process.

JobManager only ever tracks *how many* jobs are in flight (for the
concurrency-limit 429) and reads job.yaml back for status queries; it never
needs to talk to the worker process directly.
"""

from __future__ import annotations

import json
import threading
import traceback
import zipfile
from concurrent.futures import Executor, Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from greenplan.api._yamlio import atomic_write_yaml, read_yaml

JOB_FILENAME = "job.yaml"

STAGE_QUEUED = "queued"
STAGE_EXTRACTING = "extracting"
STAGE_PARSING = "parsing"
STAGE_ZONING_LAYOUT = "zoning_layout"
STAGE_EXPORTING = "exporting"
STAGE_READY = "ready"
STAGE_FAILED = "failed"
DRAFT_STATUS = "draft"

TERMINAL_STAGES = {STAGE_READY, STAGE_FAILED}
# Stages a project may be (re-)uploaded from.
UPLOADABLE_STATUSES = {DRAFT_STATUS, STAGE_FAILED}

_STAGE_PROGRESS = {
    STAGE_QUEUED: 0,
    STAGE_EXTRACTING: 5,
    STAGE_PARSING: 10,
    STAGE_ZONING_LAYOUT: 70,
    STAGE_EXPORTING: 90,
    STAGE_READY: 100,
    STAGE_FAILED: 100,
}


class JobRecord(BaseModel):
    stage: str
    progress_pct: int
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _job_path(project_dir: Path) -> Path:
    return project_dir / JOB_FILENAME


def read_job_record(project_dir: Path) -> JobRecord | None:
    """None means the project is still a draft: no job has ever run."""
    path = _job_path(project_dir)
    if not path.is_file():
        return None
    return JobRecord.model_validate(read_yaml(path))


def current_status(project_dir: Path) -> str:
    record = read_job_record(project_dir)
    return DRAFT_STATUS if record is None else record.stage


def mark_queued(project_dir: Path) -> None:
    """Written synchronously by the upload endpoint, before handing the job
    to the executor, so the response to the upload request never races the
    worker's own first job.yaml write (which may not happen for a moment if
    every worker slot is busy starting up).
    """
    _write_stage(project_dir, stage=STAGE_QUEUED, started_at=_now())


def _write_stage(project_dir: Path, *, stage: str, started_at: datetime, error: str | None = None) -> None:
    record = JobRecord(
        stage=stage,
        progress_pct=_STAGE_PROGRESS[stage],
        error=error,
        started_at=started_at,
        finished_at=_now() if stage in TERMINAL_STAGES else None,
    )
    atomic_write_yaml(_job_path(project_dir), record.model_dump(mode="json"))


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """zipfile.extractall() follows '../' path traversal ("zip slip") in
    member names; the archive comes from an untrusted upload, so every
    member's resolved path is checked to stay inside dest_dir first.
    """
    dest_dir = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (dest_dir / member.filename).resolve()
            if target != dest_dir and dest_dir not in target.parents:
                raise ValueError(f"Unsafe path in archive: {member.filename!r}")
        zf.extractall(dest_dir)


def run_processing_job(project_dir: Path) -> None:
    """The actual pipeline run: extract -> parse_folder -> plant_folder ->
    export DXF/GeoJSON/explanation. Writes job.yaml after every stage so a
    status poll always reflects real progress, and writes a failed job.yaml
    (rather than letting the exception vanish into the executor) on error.
    """
    # Imports deferred to inside the function: this module gets imported by
    # the lightweight API process on every request (for read_job_record /
    # current_status), and greenplan.pipeline pulls in the full geo stack
    # (shapely, ezdxf, geopandas) -- no reason to pay that import cost
    # outside the worker process that actually needs it.
    from greenplan.export.geojson import feature_collection_to_geojson
    from greenplan.export.planting import planting_points_to_geojson
    from greenplan.export.zones import zoning_result_to_geojson
    from greenplan.io.dxf_sink import append_planting_layer
    from greenplan.pipeline import parse_folder, plant_folder

    started_at = _now()
    raw_dir = project_dir / "raw"
    processed_dir = project_dir / "processed"

    def set_stage(stage: str) -> None:
        _write_stage(project_dir, stage=stage, started_at=started_at)

    try:
        set_stage(STAGE_EXTRACTING)
        _safe_extract_zip(project_dir / "upload.zip", raw_dir)

        set_stage(STAGE_PARSING)
        fc, _coverage = parse_folder(raw_dir)
        (processed_dir / "parsed.geojson").write_text(
            json.dumps(feature_collection_to_geojson(fc), ensure_ascii=False), encoding="utf-8"
        )

        set_stage(STAGE_ZONING_LAYOUT)
        zoning, points, explanations = plant_folder(fc)

        set_stage(STAGE_EXPORTING)
        (processed_dir / "zones.geojson").write_text(
            json.dumps(zoning_result_to_geojson(zoning), ensure_ascii=False), encoding="utf-8"
        )
        (processed_dir / "planting.geojson").write_text(
            json.dumps(planting_points_to_geojson(points), ensure_ascii=False), encoding="utf-8"
        )
        (processed_dir / "explanation.json").write_text(
            json.dumps(explanations, ensure_ascii=False), encoding="utf-8"
        )
        append_planting_layer(Path(fc.root_file), points, processed_dir / "planting.dxf")

        _write_stage(project_dir, stage=STAGE_READY, started_at=started_at)
    except BaseException as exc:
        # BaseException, not Exception: parse_folder/plant_folder use
        # SystemExit as their "fatal, user-facing input error" convention
        # (e.g. no .dxf files found) -- that's exactly the kind of error
        # this job should record and stop on, not let escape uncaught.
        traceback.print_exc()  # full traceback goes to the worker's stderr/log
        _write_stage(
            project_dir,
            stage=STAGE_FAILED,
            started_at=started_at,
            error=f"{type(exc).__name__}: {exc}",
        )


class JobManager:
    """Gates how many jobs may run at once and hands work off to an
    Executor. The executor is injected so production can use a
    ProcessPoolExecutor while tests use a ThreadPoolExecutor (same
    run_processing_job, cheaper/more deterministic to run under pytest).
    """

    def __init__(
        self,
        executor: Executor,
        max_concurrent: int,
        job_fn: Callable[[Path], None] = run_processing_job,
    ):
        self._executor = executor
        self._max_concurrent = max_concurrent
        self._job_fn = job_fn
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self._max_concurrent:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active -= 1

    def submit(self, project_dir: Path) -> Future:
        future = self._executor.submit(self._job_fn, project_dir)
        future.add_done_callback(lambda f: self._on_done(f, project_dir))
        return future

    def _on_done(self, future: Future, project_dir: Path) -> None:
        self.release()
        exc = future.exception()
        if exc is None:
            return
        # run_processing_job catches its own exceptions and writes a failed
        # job.yaml; this only fires if the worker process itself died (e.g.
        # killed/crashed) before that handler could run.
        record = read_job_record(project_dir)
        if record is not None and record.stage in TERMINAL_STAGES:
            return
        started_at = record.started_at if record is not None else _now()
        _write_stage(
            project_dir,
            stage=STAGE_FAILED,
            started_at=started_at,
            error=f"Worker process failed: {exc!r}",
        )


def reconcile_interrupted_jobs(projects_root: Path) -> None:
    """Startup housekeeping: any job.yaml left in a non-terminal stage was
    orphaned by a previous process crash/restart (no worker is coming back
    to finish it), so mark it failed rather than leaving it stuck 'parsing'
    forever.
    """
    if not projects_root.is_dir():
        return
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        record = read_job_record(project_dir)
        if record is None or record.stage in TERMINAL_STAGES:
            continue
        _write_stage(
            project_dir,
            stage=STAGE_FAILED,
            started_at=record.started_at,
            error="Interrupted by server restart",
        )
