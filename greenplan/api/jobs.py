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
import shutil
import threading
import traceback
import zipfile
from concurrent.futures import Executor, Future
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, field_validator

from greenplan.api import obstacles as obstacles_file
from greenplan.api import plantings
from greenplan.api._yamlio import atomic_write_yaml, read_yaml
from greenplan.api.config import Settings
from greenplan.api.storage import METADATA_FILENAME, ProjectRecord

JOB_FILENAME = "job.yaml"

STAGE_QUEUED = "queued"
STAGE_EXTRACTING = "extracting"
STAGE_PARSING = "parsing"
STAGE_GEOREFERENCING = "georeferencing"
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
    STAGE_GEOREFERENCING: 40,  # external, potentially slow -- surfaced as its own stage
    STAGE_ZONING_LAYOUT: 70,
    STAGE_EXPORTING: 90,
    STAGE_READY: 100,
    STAGE_FAILED: 100,
}


class UploadErrorCode(str, Enum):
    BAD_ARCHIVE = "bad_archive"  # not a zip, or corrupted
    NO_DXF_FOUND = "no_dxf_found"  # extracted fine, but zero .dxf files inside
    AMBIGUOUS_ROOT_DXF = "ambiguous_root_dxf"  # more than one plausible root drawing
    INSUFFICIENT_GEODETIC_POINTS = "insufficient_geodetic_points"  # too few/unreliable geobridge matches
    GEOREFERENCE_SERVICE_ERROR = "georeference_service_error"  # geobridge.ru request failed
    OTHER = "other"


class JobError(BaseModel):
    code: UploadErrorCode
    message: str
    # Only populated for AMBIGUOUS_ROOT_DXF: candidate root files, as paths
    # relative to the zip root (never the server's absolute filesystem path).
    candidates: list[str] | None = None


class GeoreferenceInfo(BaseModel):
    confidence: str  # "validated" | "unvalidated"
    matched_labels: list[str]
    residuals_m: dict[str, float]


class UnsafeArchiveError(ValueError):
    """Raised by _safe_extract_zip for a zip-slip ('../') path traversal
    attempt -- classified the same as a corrupted/invalid zip (see
    _classify_error): either way, the uploaded archive isn't usable as-is.
    """


class JobRecord(BaseModel):
    stage: str
    progress_pct: int
    error: JobError | None = None
    georeference: GeoreferenceInfo | None = None
    started_at: datetime
    finished_at: datetime | None = None

    @field_validator("error", mode="before")
    @classmethod
    def _upgrade_legacy_string_error(cls, value):
        """job.yaml files written before `error` became a structured JobError
        (a plain `f"{type}: {exc}"` string) still exist on disk from before
        this change -- reconcile_interrupted_jobs reads every project's
        job.yaml on every server startup, so leaving one of these unreadable
        would crash startup for *all* projects, not just the affected one.
        """
        if isinstance(value, str):
            return {"code": UploadErrorCode.OTHER, "message": value}
        return value


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


def _write_stage(
    project_dir: Path,
    *,
    stage: str,
    started_at: datetime,
    error: JobError | None = None,
    georeference: GeoreferenceInfo | None = None,
) -> None:
    record = JobRecord(
        stage=stage,
        progress_pct=_STAGE_PROGRESS[stage],
        error=error,
        georeference=georeference,
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
                raise UnsafeArchiveError(f"Unsafe path in archive: {member.filename!r}")
        zf.extractall(dest_dir)


def _classify_error(exc: BaseException, raw_dir: Path) -> JobError:
    """Turn a pipeline/extraction exception into a structured, API-facing
    error code instead of a bare exception-repr string.

    The DXF-specific exception types are imported here rather than at module
    level for the same reason as run_processing_job's own imports (see its
    docstring): this function is only ever called from within that worker
    function, so deferring the import keeps ezdxf out of the lightweight API
    process that imports this module on every status-check request.
    """
    import httpx

    from greenplan.georeference.transform import InsufficientMatchedPointsError, ResidualTooHighError
    from greenplan.io.dxf_source import NoDxfFilesError, RootDetectionError

    if isinstance(exc, (zipfile.BadZipFile, UnsafeArchiveError)):
        return JobError(code=UploadErrorCode.BAD_ARCHIVE, message=str(exc))
    if isinstance(exc, NoDxfFilesError):
        return JobError(code=UploadErrorCode.NO_DXF_FOUND, message=str(exc))
    if isinstance(exc, RootDetectionError):
        resolved_raw_dir = raw_dir.resolve()
        candidates = []
        for p in exc.candidates:
            try:
                candidates.append(str(p.relative_to(resolved_raw_dir)))
            except ValueError:
                candidates.append(p.name)
        return JobError(
            code=UploadErrorCode.AMBIGUOUS_ROOT_DXF,
            message=str(exc),
            candidates=candidates,
        )
    if isinstance(exc, (InsufficientMatchedPointsError, ResidualTooHighError)):
        return JobError(code=UploadErrorCode.INSUFFICIENT_GEODETIC_POINTS, message=str(exc))
    if isinstance(exc, httpx.HTTPError):
        return JobError(code=UploadErrorCode.GEOREFERENCE_SERVICE_ERROR, message=str(exc))
    return JobError(code=UploadErrorCode.OTHER, message=f"{type(exc).__name__}: {exc}")


def _read_bbox_user(project_dir: Path) -> tuple[float, float, float, float] | None:
    """None means "no bbox on record" -- either a project folder written
    before bbox_user existed, or (defensively) one with no metadata.yaml at
    all (some jobs.py unit tests exercise run_processing_job directly against
    a bare project_dir, never having gone through ProjectStore.create()).
    Either way, the caller treats this as "skip georeferencing", not a hard
    error: the real, enforced mandatory-ness is at the API request-schema
    level (ProjectCreateRequest.bbox_user has no default).
    """
    metadata_path = project_dir / METADATA_FILENAME
    if not metadata_path.is_file():
        return None
    return ProjectRecord.model_validate(read_yaml(metadata_path)).bbox_user


def run_processing_job(project_dir: Path) -> None:
    """The actual pipeline run: extract -> parse_folder -> georeference
    (if bbox_user is on record) -> plant_folder -> export DXF/GeoJSON/
    explanation. Writes job.yaml after every stage so a status poll always
    reflects real progress, and writes a failed job.yaml (rather than
    letting the exception vanish into the executor) on error.
    """
    # Imports deferred to inside the function: this module gets imported by
    # the lightweight API process on every request (for read_job_record /
    # current_status), and greenplan.pipeline pulls in the full geo stack
    # (shapely, ezdxf, geopandas) -- no reason to pay that import cost
    # outside the worker process that actually needs it.
    import httpx

    from greenplan.explain.builder import build_explanations
    from greenplan.export.geojson import NO_CRS_LABEL, feature_collection_to_geojson
    from greenplan.export.planting import planting_points_to_geojson
    from greenplan.export.zones import zoning_result_to_geojson
    from greenplan.georeference.apply import (
        WGS84_CRS_LABEL,
        euclidean_transform_fn,
        reproject_fn,
        transform_feature_collection,
        transform_planting_points,
        transform_zoning_result,
    )
    from greenplan.io.dxf_sink import append_planting_layer
    from greenplan.pipeline import (
        fuse_with_overture,
        georeference_feature_collection,
        load_default_norms,
        load_default_planting_rules,
        parse_folder,
        plant_folder,
    )

    started_at = _now()
    raw_dir = project_dir / "raw"
    processed_dir = project_dir / "processed"

    def set_stage(stage: str) -> None:
        _write_stage(project_dir, stage=stage, started_at=started_at)

    def write_parsed(fc, crs_label: str, norms) -> dict:
        """parsed.geojson + obstacles.geojson (not yet clipped to the site:
        that needs zoning); returns the obstacles for the final write."""
        parsed = feature_collection_to_geojson(fc, crs=crs_label)
        (processed_dir / obstacles_file.PARSED_FILENAME).write_text(
            json.dumps(parsed, ensure_ascii=False), encoding="utf-8"
        )
        unclipped = obstacles_file.obstacles_geojson(parsed, norms)
        obstacles_file.write_obstacles(processed_dir, unclipped)
        return unclipped

    try:
        set_stage(STAGE_EXTRACTING)
        # A re-upload is allowed from 'draft'/'failed' (see UPLOADABLE_STATUSES),
        # but zipfile.extractall() only ever adds/overwrites -- it never
        # removes -- so without clearing these first, a file present in an
        # earlier failed attempt but absent from the new zip would linger
        # forever and keep influencing processing (e.g. a stale extra
        # candidate that makes root detection see a file the new upload
        # never even contained).
        shutil.rmtree(raw_dir, ignore_errors=True)
        shutil.rmtree(processed_dir, ignore_errors=True)
        raw_dir.mkdir()
        processed_dir.mkdir()
        _safe_extract_zip(project_dir / "upload.zip", raw_dir)

        set_stage(STAGE_PARSING)
        fc, _coverage = parse_folder(raw_dir)
        norms = load_default_norms()
        # Written in the drawing's frame right away (and replaced by WGS84
        # below), so a project that fails at georeferencing still has its
        # obstacles -- the frontend's manual-georeferencing fallback uses them.
        obstacles = write_parsed(fc, NO_CRS_LABEL, norms)

        bbox_user = _read_bbox_user(project_dir)
        geo_result = None
        if bbox_user is not None:
            set_stage(STAGE_GEOREFERENCING)
            settings = Settings()
            with httpx.Client() as client:
                fc, geo_result = georeference_feature_collection(
                    fc, bbox_user, client=client,
                    timeout=settings.geobridge_timeout_s,
                    base_url=settings.geobridge_base_url,
                    utm_crs=settings.georeference_utm_epsg,
                    min_matched_points=settings.georeference_min_points,
                    residual_threshold_m=settings.georeference_residual_threshold_m,
                )
            fc, _fusion = fuse_with_overture(fc, geo_result.utm_crs, settings.resolved_overture_cache_dir)

        if geo_result is not None:
            crs_label = WGS84_CRS_LABEL
            to_wgs84 = reproject_fn(geo_result.utm_crs)
            obstacles = write_parsed(transform_feature_collection(fc, to_wgs84), crs_label, norms)
        else:
            crs_label = NO_CRS_LABEL

        set_stage(STAGE_ZONING_LAYOUT)
        planting_rules = load_default_planting_rules()
        zoning, points, explanations = plant_folder(fc, norms=norms, planting_rules=planting_rules)

        set_stage(STAGE_EXPORTING)
        if geo_result is not None:
            zoning_out = transform_zoning_result(zoning, to_wgs84)
            points_out = transform_planting_points(points, to_wgs84)
            points_local = transform_planting_points(points, euclidean_transform_fn(geo_result.utm_to_local))
            explanations_out = build_explanations(points_local, planting_rules)
            dxf_points = points_local
        else:
            zoning_out, points_out, explanations_out, dxf_points = zoning, points, explanations, points

        (processed_dir / "zones.geojson").write_text(
            json.dumps(zoning_result_to_geojson(zoning_out, crs=crs_label), ensure_ascii=False), encoding="utf-8"
        )
        site = zoning_out.base_area
        extent = None if site is None or site.is_empty else site.bounds
        obstacles_file.write_obstacles(processed_dir, obstacles_file.obstacles_geojson(obstacles, norms, extent))
        (processed_dir / "planting.geojson").write_text(
            json.dumps(planting_points_to_geojson(points_out, crs=crs_label), ensure_ascii=False), encoding="utf-8"
        )
        (processed_dir / "explanation.json").write_text(
            json.dumps(explanations_out, ensure_ascii=False), encoding="utf-8"
        )
        append_planting_layer(Path(fc.root_file), dxf_points, processed_dir / "planting.dxf")
        _write_export_context(processed_dir, raw_dir, Path(fc.root_file), geo_result)
        plantings.create_first_version(processed_dir)

        georeference_info = (
            GeoreferenceInfo(
                confidence=geo_result.confidence,
                matched_labels=geo_result.matched_labels,
                residuals_m=geo_result.residuals_m,
            )
            if geo_result is not None
            else None
        )
        _write_stage(project_dir, stage=STAGE_READY, started_at=started_at, georeference=georeference_info)
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
            error=_classify_error(exc, raw_dir),
        )


def _write_export_context(processed_dir: Path, raw_dir: Path, root_file: Path, geo_result) -> None:
    """What run_version_export_job needs later to put edited (WGS84) points
    back into the root drawing's local frame. The fitted transform only
    exists during this job, so it has to be saved now.
    """
    try:
        root_rel = str(root_file.resolve().relative_to(raw_dir.resolve()))
    except ValueError:
        root_rel = str(root_file.resolve())
    context: dict = {"root_file": root_rel}
    if geo_result is not None:
        context.update(
            frame="utm",
            utm_crs=geo_result.utm_crs,
            utm_to_local=geo_result.utm_to_local.params.tolist(),
        )
    else:
        context["frame"] = "local"
    atomic_write_yaml(processed_dir / plantings.EXPORT_CONTEXT_FILENAME, context)


def run_version_export_job(version_dir: Path) -> None:
    """Build planting.dxf and explanation.json for one saved planting
    version (2+) and record the outcome in its metadata.yaml export_status.
    Same rules as run_processing_job: top-level, picklable, heavy imports
    deferred, never lets an exception escape.
    """
    import numpy as np
    from skimage.transform import EuclideanTransform

    from greenplan.explain.builder import build_explanations
    from greenplan.export.planting import geojson_to_planting_points
    from greenplan.georeference.apply import euclidean_transform_fn, reproject_fn, transform_planting_points
    from greenplan.io.dxf_sink import append_planting_layer
    from greenplan.pipeline import load_default_planting_rules

    processed_dir = version_dir.parent.parent
    raw_dir = processed_dir.parent / "raw"
    try:
        context = read_yaml(processed_dir / plantings.EXPORT_CONTEXT_FILENAME)

        def load_points(vdir: Path):
            data = json.loads((vdir / plantings.GEOJSON_FILENAME).read_text(encoding="utf-8"))
            added = {
                f["properties"]["id"]: f["properties"]["added_in_version"]
                for f in data["features"]
                if "added_in_version" in f["properties"]
            }
            return geojson_to_planting_points(data), added

        points, added_in_version = load_points(version_dir)
        original, _ = load_points(plantings.version_dir(processed_dir, 1))
        if context["frame"] == "utm":
            to_utm = reproject_fn("EPSG:4326", context["utm_crs"])
            to_local = euclidean_transform_fn(EuclideanTransform(matrix=np.array(context["utm_to_local"])))

            def to_drawing(pts):
                return transform_planting_points(transform_planting_points(pts, to_utm), to_local)

            points, original = to_drawing(points), to_drawing(original)

        tmp_dxf = version_dir / (plantings.DXF_FILENAME + ".tmp")
        append_planting_layer(raw_dir / context["root_file"], points, tmp_dxf)
        tmp_dxf.replace(version_dir / plantings.DXF_FILENAME)

        explanations = build_explanations(
            points,
            load_default_planting_rules(),
            original_points={p.id: p for p in original},
            added_in_version=added_in_version,
        )
        (version_dir / plantings.EXPLANATION_FILENAME).write_text(
            json.dumps(explanations, ensure_ascii=False), encoding="utf-8"
        )
        plantings.set_export_status(version_dir, plantings.EXPORT_READY)
    except BaseException as exc:
        traceback.print_exc()
        plantings.set_export_status(
            version_dir,
            plantings.EXPORT_FAILED,
            plantings.ExportError(code="export_failed", message=f"{type(exc).__name__}: {exc}"),
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
        export_fn: Callable[[Path], None] = run_version_export_job,
    ):
        self._executor = executor
        self._max_concurrent = max_concurrent
        self._job_fn = job_fn
        self._export_fn = export_fn
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

    def submit_version_export(self, version_dir: Path) -> Future:
        """Caller must have acquired a slot (try_acquire) and marked the
        version pending; shares the concurrency limit with processing jobs.
        """
        future = self._executor.submit(self._export_fn, version_dir)
        future.add_done_callback(lambda f: self._on_export_done(f, version_dir))
        return future

    def _on_export_done(self, future: Future, version_dir: Path) -> None:
        self.release()
        exc = future.exception()
        if exc is None:
            return
        # Same as _on_done: only reached if the worker process itself died.
        version = plantings.read_version_metadata(version_dir)
        if version is not None and version.export_status == plantings.EXPORT_PENDING:
            plantings.set_export_status(
                version_dir,
                plantings.EXPORT_FAILED,
                plantings.ExportError(code="export_failed", message=f"Worker process failed: {exc!r}"),
            )

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
            error=JobError(code=UploadErrorCode.OTHER, message=f"Worker process failed: {exc!r}"),
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
            error=JobError(code=UploadErrorCode.OTHER, message="Interrupted by server restart"),
        )
