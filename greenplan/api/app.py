"""FastAPI wrapper over greenplan.pipeline. `create_app()` is the factory
tests use (to inject an isolated data_dir and a cheaper test executor); the
module-level `app` is the production entrypoint for `uvicorn
greenplan.api.app:app`.
"""

from __future__ import annotations

from concurrent.futures import Executor, ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from greenplan.api import jobs
from greenplan.api.config import Settings
from greenplan.api.jobs import JobManager
from greenplan.api.schemas import JobStatus, ProjectCreateRequest, ProjectResponse, ProjectUpdateRequest
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
    record = store.create(body.name, body.description)
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


@router.get("/{project_id}/planting")
def get_planting(project_id: str, request: Request) -> Response:
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    body = (project_dir / "processed" / "planting.geojson").read_text(encoding="utf-8")
    return Response(content=body, media_type="application/geo+json")


@router.get("/{project_id}/explanation")
def get_explanation(project_id: str, request: Request) -> Response:
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    body = (project_dir / "processed" / "explanation.json").read_text(encoding="utf-8")
    return Response(content=body, media_type="application/json")


@router.get("/{project_id}/dxf")
def get_dxf(project_id: str, request: Request) -> FileResponse:
    project_dir = _require_ready_dir(request.app.state.store, project_id)
    return FileResponse(
        project_dir / "processed" / "planting.dxf",
        media_type="application/dxf",
        filename="planting.dxf",
    )


def create_app(
    settings: Settings | None = None,
    executor_factory: Callable[[], Executor] | None = None,
    job_fn: Callable[[Path], None] | None = None,
) -> FastAPI:
    """`executor_factory` and `job_fn` exist so tests can swap in a
    ThreadPoolExecutor and a controllable fake job body instead of the
    production ProcessPoolExecutor + real pipeline run -- same JobManager
    gating/callback logic either way.
    """
    settings = settings or Settings()
    executor_factory = executor_factory or (lambda: ProcessPoolExecutor(max_workers=settings.max_concurrent_jobs))
    job_fn = job_fn or jobs.run_processing_job

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = ProjectStore(settings.data_dir)
        store.load()
        jobs.reconcile_interrupted_jobs(store.projects_dir)

        executor = executor_factory()
        app.state.settings = settings
        app.state.store = store
        app.state.jobs = JobManager(executor, settings.max_concurrent_jobs, job_fn=job_fn)
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
    )
    app.include_router(router)
    return app


app = create_app()
