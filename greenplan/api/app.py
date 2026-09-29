"""FastAPI wrapper over greenplan.pipeline. `create_app()` is the factory
tests use (to inject an isolated data_dir and a cheaper test executor); the
module-level `app` is the production entrypoint for `uvicorn
greenplan.api.app:app`.
"""

from __future__ import annotations

import re
from concurrent.futures import Executor, ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from greenplan.api import jobs, plantings
from greenplan.api.config import Settings
from greenplan.api.jobs import JobManager
from greenplan.api.plantings import PlantingEdit, PlantingStore, PlantingVersion
from greenplan.api.schemas import (
    ErrorMessage,
    ErrorWithCode,
    ExplanationEntry,
    ExportPendingResponse,
    JobStatus,
    PlantingEditResponse,
    PlantingFeatureCollection,
    ProjectCreateRequest,
    ProjectResponse,
    ProjectUpdateRequest,
)
from greenplan.api.storage import ProjectNotFoundError, ProjectRecord, ProjectStore

router = APIRouter(prefix="/projects", tags=["projects"])


def _to_response(store: ProjectStore, record: ProjectRecord) -> ProjectResponse:
    job_record = jobs.read_job_record(store.project_dir(record.id))
    status = jobs.DRAFT_STATUS if job_record is None else job_record.stage
    return ProjectResponse(
        id=record.id,
        name=record.name,
        description=record.description,
        created_at=record.created_at,
        updated_at=record.updated_at,
        status=status,
        job=JobStatus.from_record(job_record),
    )


def _get_project_or_404(store: ProjectStore, project_id: str) -> ProjectRecord:
    record = store.get(project_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return record


def _require_ready_dir(store: ProjectStore, project_id: str) -> Path:
    _get_project_or_404(store, project_id)
    project_dir = store.project_dir(project_id)
    status = jobs.current_status(project_dir)
    if status != jobs.STAGE_READY:
        raise HTTPException(status_code=404, detail=f"Project data not available yet (status: {status})")
    return project_dir


@router.get("", response_model=list[ProjectResponse])
def list_projects(request: Request) -> list[ProjectResponse]:
    store: ProjectStore = request.app.state.store
    return [_to_response(store, record) for record in store.list_active()]


@router.post("", response_model=ProjectResponse, status_code=201)
def create_project(body: ProjectCreateRequest, request: Request) -> ProjectResponse:
    store: ProjectStore = request.app.state.store
    record = store.create(body.name, body.description, body.bbox_user)
    return _to_response(store, record)


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, request: Request) -> ProjectResponse:
    store: ProjectStore = request.app.state.store
    record = _get_project_or_404(store, project_id)
    return _to_response(store, record)


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(project_id: str, body: ProjectUpdateRequest, request: Request) -> ProjectResponse:
    store: ProjectStore = request.app.state.store
    try:
        record = store.update(project_id, body.name, body.description)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    return _to_response(store, record)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, request: Request) -> Response:
    store: ProjectStore = request.app.state.store
    try:
        store.soft_delete(project_id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    return Response(status_code=204)


@router.post("/{project_id}/upload", response_model=ProjectResponse, status_code=202)
async def upload_project(project_id: str, request: Request) -> ProjectResponse:
    store: ProjectStore = request.app.state.store
    job_manager: JobManager = request.app.state.jobs
    settings: Settings = request.app.state.settings

    record = _get_project_or_404(store, project_id)
    project_dir = store.project_dir(project_id)

    status = jobs.current_status(project_dir)
    if status not in jobs.UPLOADABLE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Project is not uploadable in status '{status}'")

    # Gate on the concurrency limit before consuming a single byte of the
    # (up to 100MB) body, so a rejected request costs ~nothing.
    if not job_manager.try_acquire():
        raise HTTPException(status_code=429, detail="Too many concurrent processing jobs, try again later")

    dest = project_dir / "upload.zip"
    tmp_dest = project_dir / "upload.zip.part"
    try:
        size = 0
        with open(tmp_dest, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="Upload exceeds the maximum allowed size")
                f.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="No file body provided")
        tmp_dest.replace(dest)
        jobs.mark_queued(project_dir)
        job_manager.submit(project_dir)
    except Exception:
        job_manager.release()
        tmp_dest.unlink(missing_ok=True)
        raise

    return _to_response(store, record)


@router.get("/{project_id}/zones")
def get_zones(project_id: str, request: Request) -> Response:
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    body = (project_dir / "processed" / "zones.geojson").read_text(encoding="utf-8")
    return Response(content=body, media_type="application/geo+json")


# IANA-registered type for DXF (also what mimetypes/`/etc/mime.types` give).
DXF_MEDIA_TYPE = "image/vnd.dxf"


class GeoJSONResponse(Response):
    media_type = "application/geo+json"


class DXFResponse(FileResponse):
    media_type = DXF_MEDIA_TYPE


def _dxf_filename(project_name: str, version_id: int) -> str:
    """Download name for a version's DXF, e.g. "Сквер — версия 2.dxf".
    Project names are free user input: characters that are invalid in file
    names on Windows/macOS/Linux (and control characters) become "_".
    Non-ASCII is fine -- Starlette sends it as RFC 5987 `filename*`.
    """
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", project_name)
    safe = " ".join(safe.split()).strip(" .")[:120] or "план посадок"
    return f"{safe} — версия {version_id}.dxf"


_NOT_FOUND = {
    404: {"model": ErrorMessage, "description": "Project not found, not `ready` yet, or no such version."}
}
_EXPORT_RESPONSES = {
    **_NOT_FOUND,
    202: {
        "model": ExportPendingResponse,
        "description": "The file is being generated (it starts on save, or on this request if it "
        "hadn't yet). Poll `GET /projects/{id}/plantings` until the version's `export_status` is "
        "`ready`, then request the file again. Has a `Retry-After` header (seconds).",
    },
    409: {
        "model": ErrorWithCode,
        "description": "`transform_unavailable`: the project was processed before edited versions "
        "could be exported; only version 1 has a DXF/explanation.",
    },
    429: {
        "model": ErrorMessage,
        "description": "Generation is needed but all worker slots are busy; try again later.",
    },
    500: {
        "model": ErrorWithCode,
        "description": "Generation failed (`export_failed`); not retried automatically.",
    },
}
# FastAPI files every `model` response under the route's own media type, so
# the GeoJSON/DXF routes declare response_class=Response (no media type):
# errors then fall back to application/json, and 200 is declared explicitly.
_GEOJSON_CONTENT = {
    200: {
        "description": "GeoJSON FeatureCollection of Point features.",
        "content": {
            "application/geo+json": {"schema": {"$ref": "#/components/schemas/PlantingFeatureCollection"}}
        },
    }
}
_DXF_CONTENT = {
    200: {
        "description": "The root drawing with the planting added on the `GREENING_PROPOSED` layer "
        "(one circle per point; XDATA `GREENPLAN`: plant_type, rule_id, id, kind). "
        'Sent as an attachment named after the project, e.g. `Сквер — версия 2.dxf` '
        "(non-ASCII names via `filename*`, RFC 5987).",
        "content": {DXF_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
    }
}


def _latest_version_id(request: Request, project_dir: Path) -> int:
    return request.app.state.plantings.latest_version(project_dir / "processed").id


def _get_version_or_404(request: Request, project_dir: Path, version_id: int) -> PlantingVersion:
    try:
        return request.app.state.plantings.get_version(project_dir / "processed", version_id)
    except plantings.VersionNotFoundError:
        raise HTTPException(status_code=404, detail="Planting version not found")


def _start_export(request: Request, project_dir: Path, version_id: int) -> bool:
    """Start generating a version's DXF/explanation if a worker slot is free."""
    job_manager: JobManager = request.app.state.jobs
    if not job_manager.try_acquire():
        return False
    vdir = plantings.version_dir(project_dir / "processed", version_id)
    try:
        plantings.set_export_status(vdir, plantings.EXPORT_PENDING)
        job_manager.submit_version_export(vdir)
    except Exception:
        job_manager.release()
        plantings.set_export_status(vdir, plantings.EXPORT_NONE)
        raise
    return True


def _version_file(request: Request, project_dir: Path, version_id: int, filename: str) -> Path | JSONResponse:
    """A version's export file, or a 202 response while it is (being) generated."""
    version = _get_version_or_404(request, project_dir, version_id)
    path = plantings.version_dir(project_dir / "processed", version_id) / filename
    if version.export_status == plantings.EXPORT_READY:
        return path
    if version.export_status == plantings.EXPORT_FAILED:
        raise HTTPException(status_code=500, detail=version.export_error.model_dump() if version.export_error else None)
    if version.export_status == plantings.EXPORT_NONE:
        if not (project_dir / "processed" / plantings.EXPORT_CONTEXT_FILENAME).is_file():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "transform_unavailable",
                    "message": "Project was processed before edited versions could be exported; "
                    "only version 1 has a DXF/explanation",
                },
            )
        if not _start_export(request, project_dir, version_id):
            raise HTTPException(status_code=429, detail="Too many concurrent processing jobs, try again later")
    return JSONResponse(
        status_code=202,
        content=ExportPendingResponse(version=version_id, export_status=plantings.EXPORT_PENDING).model_dump(),
        headers={"Retry-After": "5"},
    )


def _dxf_response(request: Request, project_dir: Path, version_id: int) -> Response:
    result = _version_file(request, project_dir, version_id, plantings.DXF_FILENAME)
    if isinstance(result, Response):
        return result
    project_name = request.app.state.store.get(project_dir.name).name
    return DXFResponse(result, filename=_dxf_filename(project_name, version_id))


def _explanation_response(request: Request, project_dir: Path, version_id: int) -> Response:
    result = _version_file(request, project_dir, version_id, plantings.EXPLANATION_FILENAME)
    if isinstance(result, Response):
        return result
    return Response(content=result.read_text(encoding="utf-8"), media_type="application/json")


def _geojson_response(request: Request, project_dir: Path, version_id: int) -> Response:
    _get_version_or_404(request, project_dir, version_id)
    body = request.app.state.plantings.read_geojson(project_dir / "processed", version_id)
    return GeoJSONResponse(content=body)


# The routes below return stored files as-is (no re-serialization); their
# response_model only documents the shape in OpenAPI.


@router.get(
    "/{project_id}/planting",
    response_model=PlantingFeatureCollection,  # registers the schema _GEOJSON_CONTENT refers to
    response_class=Response,
    responses={**_GEOJSON_CONTENT, **_NOT_FOUND},
    summary="Latest planting version (GeoJSON)",
)
def get_planting(project_id: str, request: Request) -> Response:
    """Same as `GET /plantings/{n}` for the latest version. Before any edit,
    that is version 1, the automatic layout."""
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    return _geojson_response(request, project_dir, _latest_version_id(request, project_dir))


@router.get(
    "/{project_id}/explanation",
    response_model=list[ExplanationEntry],
    responses=_EXPORT_RESPONSES,
    summary="Explanation for the latest planting version",
)
def get_explanation(project_id: str, request: Request) -> Response:
    """Same as `GET /plantings/{n}/explanation` for the latest version."""
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    return _explanation_response(request, project_dir, _latest_version_id(request, project_dir))


@router.get(
    "/{project_id}/dxf",
    response_class=Response,
    responses={**_DXF_CONTENT, **_EXPORT_RESPONSES},
    summary="DXF for the latest planting version",
)
def get_dxf(project_id: str, request: Request) -> Response:
    """Same as `GET /plantings/{n}/dxf` for the latest version."""
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    return _dxf_response(request, project_dir, _latest_version_id(request, project_dir))


@router.get(
    "/{project_id}/plantings",
    response_model=list[PlantingVersion],
    responses=_NOT_FOUND,
    summary="List planting versions",
)
def list_plantings(project_id: str, request: Request) -> list[PlantingVersion]:
    """All versions, oldest first. Version 1 is the automatic layout; each
    saved edit adds a version. History is linear: editing any version
    appends a new one (`based_on` says which it was edited from)."""
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    return request.app.state.plantings.list_versions(project_dir / "processed")


@router.get(
    "/{project_id}/plantings/{version_id}",
    response_model=PlantingFeatureCollection,  # registers the schema _GEOJSON_CONTENT refers to
    response_class=Response,
    responses={**_GEOJSON_CONTENT, **_NOT_FOUND},
    summary="Planting version (GeoJSON)",
)
def get_planting_version(project_id: str, version_id: int, request: Request) -> Response:
    """All points of one version, WGS84."""
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    return _geojson_response(request, project_dir, version_id)


@router.get(
    "/{project_id}/plantings/{version_id}/dxf",
    response_class=Response,
    responses={**_DXF_CONTENT, **_EXPORT_RESPONSES},
    summary="DXF for a planting version",
)
def get_planting_version_dxf(project_id: str, version_id: int, request: Request) -> Response:
    """Version 1's DXF always exists. For later versions it is generated in
    the background (see the `202` response)."""
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    return _dxf_response(request, project_dir, version_id)


@router.get(
    "/{project_id}/plantings/{version_id}/explanation",
    response_model=list[ExplanationEntry],
    responses=_EXPORT_RESPONSES,
    summary="Explanation for a planting version",
)
def get_planting_version_explanation(project_id: str, version_id: int, request: Request) -> Response:
    """One entry per point: which rule placed it and, for edited versions,
    what was changed by hand. Generated together with the DXF (see the
    `202` response)."""
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    return _explanation_response(request, project_dir, version_id)


@router.post(
    "/{project_id}/plantings/{version_id}/edit",
    response_model=PlantingEditResponse,
    status_code=201,
    responses={
        **_NOT_FOUND,
        422: {
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}},
            "description": "Invalid edit; nothing is saved. Same `{loc, msg, type}` shape for schema "
            "errors and for these edit-specific `type`s: `empty_edit`, `unknown_id`, `duplicate_id`, "
            "`duplicate_client_id`, `update_delete_conflict`, `outside_project_bbox`, "
            "`coordinates_not_finite`, `lon_lat_pair`, `nothing_to_update`.",
        },
    },
    summary="Save an edit of a version as a new version",
)
def edit_planting_version(
    project_id: str, version_id: int, body: PlantingEdit, request: Request
) -> PlantingEditResponse:
    """Applies `add`/`update`/`delete` to version `version_id` and saves the
    result as a new version (the base version is never changed). New points
    get server ids, returned in `id_map` by `client_id`. Zones are not
    checked on the server. Generation of the new version's DXF/explanation
    starts right away if a worker slot is free (`version.export_status`
    is then `pending`, otherwise `none`)."""
    store: ProjectStore = request.app.state.store
    project_dir = _require_ready_dir(store, project_id)
    processed_dir = project_dir / "processed"
    _get_version_or_404(request, project_dir, version_id)
    try:
        version, id_map = request.app.state.plantings.create_edited_version(
            processed_dir, version_id, body, store.get(project_id).bbox_user
        )
    except plantings.EditValidationError as exc:
        raise RequestValidationError(exc.errors)
    # Start the DXF/explanation export right away if a slot is free; otherwise
    # it starts on the first request for either file.
    if (processed_dir / plantings.EXPORT_CONTEXT_FILENAME).is_file() and _start_export(
        request, project_dir, version.id
    ):
        version = plantings.read_version_metadata(plantings.version_dir(processed_dir, version.id))
    return PlantingEditResponse(version=version, id_map=id_map)


def create_app(
    settings: Settings | None = None,
    executor_factory: Callable[[], Executor] | None = None,
    job_fn: Callable[[Path], None] | None = None,
    export_fn: Callable[[Path], None] | None = None,
) -> FastAPI:
    """`executor_factory`, `job_fn` and `export_fn` exist so tests can swap
    in a ThreadPoolExecutor and controllable fake job bodies instead of the
    production ProcessPoolExecutor + real pipeline run -- same JobManager
    gating/callback logic either way.
    """
    settings = settings or Settings()
    executor_factory = executor_factory or (lambda: ProcessPoolExecutor(max_workers=settings.max_concurrent_jobs))
    job_fn = job_fn or jobs.run_processing_job
    export_fn = export_fn or jobs.run_version_export_job

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = ProjectStore(settings.data_dir)
        store.load()
        jobs.reconcile_interrupted_jobs(store.projects_dir)
        plantings.reconcile_interrupted_exports(store.projects_dir)

        executor = executor_factory()
        app.state.settings = settings
        app.state.store = store
        app.state.jobs = JobManager(executor, settings.max_concurrent_jobs, job_fn=job_fn, export_fn=export_fn)
        app.state.plantings = PlantingStore()
        try:
            yield
        finally:
            executor.shutdown(wait=False)

    app = FastAPI(title="greenplan API", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
        # Lets a cross-origin frontend read the DXF download's file name.
        expose_headers=["Content-Disposition"],
    )
    app.include_router(router)
    return app


app = create_app()
