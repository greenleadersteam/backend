import io
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

import ezdxf
import pytest
from fastapi.testclient import TestClient

from greenplan.api.app import create_app
from greenplan.api.config import Settings


def _empty_dxf_zip_bytes(tmp_path) -> bytes:
    """A minimal but genuine DXF (no matching layers, no entities) zipped
    up: runs through the real parse_folder -> plant_folder -> dxf_sink
    pipeline end to end without needing any real pilot-object data.

    ezdxf only writes its (text) DXF format to a real file/text stream, not
    an in-memory bytes buffer, hence the throwaway file under tmp_path.
    """
    dxf_path = tmp_path / f"root-{uuid.uuid4().hex}.dxf"
    ezdxf.new("R2010").saveas(dxf_path)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.write(dxf_path, "root.dxf")
    return zip_buf.getvalue()


def _no_dxf_zip_bytes() -> bytes:
    """A zip with no .dxf inside: exercises parse_folder's SystemExit
    failure path end to end.
    """
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("readme.txt", "no dxf here")
    return zip_buf.getvalue()


def _make_app(tmp_path, max_concurrent_jobs=2, max_upload_mb=100, job_fn=None):
    settings = Settings(
        data_dir=tmp_path / "data", max_concurrent_jobs=max_concurrent_jobs, max_upload_mb=max_upload_mb
    )
    return create_app(
        settings=settings,
        executor_factory=lambda: ThreadPoolExecutor(max_workers=max_concurrent_jobs),
        job_fn=job_fn,
    )


def _wait_for_terminal(client, project_id, timeout=10.0):
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        data = client.get(f"/projects/{project_id}").json()
        if data["status"] in ("ready", "failed"):
            return data
        time.sleep(0.05)
    pytest.fail(f"job did not reach a terminal state in time, last seen: {data}")


@pytest.fixture
def client(tmp_path):
    with TestClient(_make_app(tmp_path)) as c:
        yield c


def test_create_project_starts_as_draft(client):
    r = client.post("/projects", json={"name": "A", "description": "d"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "draft"
    assert body["name"] == "A"
    assert body["description"] == "d"
    assert body["job"]["stage"] == "draft"


def test_list_projects_excludes_deleted(client):
    a = client.post("/projects", json={"name": "A"}).json()["id"]
    client.post("/projects", json={"name": "B"})
    client.delete(f"/projects/{a}")

    names = {p["name"] for p in client.get("/projects").json()}
    assert names == {"B"}


def test_get_missing_project_404(client):
    assert client.get("/projects/does-not-exist").status_code == 404


def test_update_metadata_only(client):
    pid = client.post("/projects", json={"name": "A", "description": "d"}).json()["id"]

    r = client.patch(f"/projects/{pid}", json={"description": "new desc"})

    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "A"
    assert body["description"] == "new desc"


def test_update_missing_project_404(client):
    assert client.patch("/projects/does-not-exist", json={"name": "x"}).status_code == 404


def test_soft_delete_then_404_everywhere(client):
    pid = client.post("/projects", json={"name": "A"}).json()["id"]

    assert client.delete(f"/projects/{pid}").status_code == 204
    assert client.get(f"/projects/{pid}").status_code == 404
    assert client.delete(f"/projects/{pid}").status_code == 404
    assert all(p["id"] != pid for p in client.get("/projects").json())


def test_data_endpoints_404_before_ready(client):
    pid = client.post("/projects", json={"name": "A"}).json()["id"]
    for path in ("zones", "planting", "explanation", "dxf"):
        assert client.get(f"/projects/{pid}/{path}").status_code == 404


def test_upload_to_missing_project_404(client):
    assert client.post("/projects/does-not-exist/upload", content=b"x").status_code == 404


def test_upload_too_large_rejected(tmp_path):
    with TestClient(_make_app(tmp_path, max_upload_mb=1)) as client:
        pid = client.post("/projects", json={"name": "A"}).json()["id"]
        oversized = b"0" * (2 * 1024 * 1024)

        r = client.post(f"/projects/{pid}/upload", content=oversized)

        assert r.status_code == 413
        # a rejected upload must not leave the project stuck as if queued
        assert client.get(f"/projects/{pid}").json()["status"] == "draft"


def test_full_upload_success_flow(client, tmp_path):
    pid = client.post("/projects", json={"name": "A"}).json()["id"]

    r = client.post(f"/projects/{pid}/upload", content=_empty_dxf_zip_bytes(tmp_path))
    assert r.status_code == 202
    assert r.json()["status"] in ("queued", "extracting", "parsing", "zoning_layout", "exporting", "ready")

    data = _wait_for_terminal(client, pid)
    assert data["status"] == "ready"
    assert data["job"]["progress_pct"] == 100
    assert data["job"]["error"] is None

    zones = client.get(f"/projects/{pid}/zones")
    assert zones.status_code == 200
    assert zones.json()["type"] == "FeatureCollection"

    planting = client.get(f"/projects/{pid}/planting")
    assert planting.status_code == 200

    explanation = client.get(f"/projects/{pid}/explanation")
    assert explanation.status_code == 200

    dxf = client.get(f"/projects/{pid}/dxf")
    assert dxf.status_code == 200
    assert dxf.content.startswith(b"  0\n") or dxf.content.startswith(b"  0\r\n")


def test_upload_failure_marks_project_failed(client):
    pid = client.post("/projects", json={"name": "A"}).json()["id"]

    r = client.post(f"/projects/{pid}/upload", content=_no_dxf_zip_bytes())
    assert r.status_code == 202

    data = _wait_for_terminal(client, pid)
    assert data["status"] == "failed"
    assert data["job"]["error"]["code"] == "no_dxf_found"
    assert "No .dxf files" in data["job"]["error"]["message"]


def test_upload_non_zip_body_reports_bad_archive(client):
    pid = client.post("/projects", json={"name": "A"}).json()["id"]

    r = client.post(f"/projects/{pid}/upload", content=b"this is not a zip file at all")
    assert r.status_code == 202

    data = _wait_for_terminal(client, pid)
    assert data["status"] == "failed"
    assert data["job"]["error"]["code"] == "bad_archive"


def _ambiguous_root_zip_bytes(tmp_path) -> bytes:
    """Two independent DXFs with real content and no xref link either way --
    exercises detect_root's genuinely-ambiguous path end to end, and checks
    that reported candidates are zip-root-relative, not server-absolute.
    """
    a_path = tmp_path / f"a-{uuid.uuid4().hex}.dxf"
    b_path = tmp_path / f"b-{uuid.uuid4().hex}.dxf"
    doc_a = ezdxf.new("R2010")
    doc_a.modelspace().add_line((0, 0), (1, 1))
    doc_a.saveas(a_path)
    doc_b = ezdxf.new("R2010")
    doc_b.modelspace().add_line((0, 0), (1, 1))
    doc_b.saveas(b_path)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.write(a_path, "sub/genplan.dxf")
        zf.write(b_path, "dendroplan.dxf")
    return zip_buf.getvalue()


def test_upload_ambiguous_root_reports_candidates_relative_to_zip_root(client, tmp_path):
    pid = client.post("/projects", json={"name": "A"}).json()["id"]

    r = client.post(f"/projects/{pid}/upload", content=_ambiguous_root_zip_bytes(tmp_path))
    assert r.status_code == 202

    data = _wait_for_terminal(client, pid)
    assert data["status"] == "failed"
    error = data["job"]["error"]
    assert error["code"] == "ambiguous_root_dxf"
    assert set(error["candidates"]) == {"sub/genplan.dxf", "dendroplan.dxf"}


def test_upload_empty_body_returns_400_without_queuing_a_job(client):
    pid = client.post("/projects", json={"name": "A"}).json()["id"]

    r = client.post(f"/projects/{pid}/upload", content=b"")
    assert r.status_code == 400

    assert client.get(f"/projects/{pid}").json()["status"] == "draft"


def test_reupload_allowed_after_a_failed_run(client):
    pid = client.post("/projects", json={"name": "A"}).json()["id"]
    client.post(f"/projects/{pid}/upload", content=_no_dxf_zip_bytes())
    _wait_for_terminal(client, pid)

    r = client.post(f"/projects/{pid}/upload", content=_no_dxf_zip_bytes())

    assert r.status_code == 202


def test_upload_rejected_while_processing_and_after_ready(client, tmp_path):
    pid = client.post("/projects", json={"name": "A"}).json()["id"]
    zip_bytes = _empty_dxf_zip_bytes(tmp_path)
    client.post(f"/projects/{pid}/upload", content=zip_bytes)

    conflict = client.post(f"/projects/{pid}/upload", content=zip_bytes)
    assert conflict.status_code == 409

    _wait_for_terminal(client, pid)

    conflict_after_ready = client.post(f"/projects/{pid}/upload", content=zip_bytes)
    assert conflict_after_ready.status_code == 409


def test_concurrency_limit_returns_429_until_a_slot_frees(tmp_path):
    release = threading.Event()

    def blocking_job(project_dir):
        release.wait(timeout=5)

    with TestClient(_make_app(tmp_path, max_concurrent_jobs=1, job_fn=blocking_job)) as client:
        first = client.post("/projects", json={"name": "A"}).json()["id"]
        second = client.post("/projects", json={"name": "B"}).json()["id"]

        r1 = client.post(f"/projects/{first}/upload", content=_no_dxf_zip_bytes())
        assert r1.status_code == 202

        r2 = client.post(f"/projects/{second}/upload", content=_no_dxf_zip_bytes())
        assert r2.status_code == 429

        release.set()

        deadline = time.time() + 5
        last_status = None
        while time.time() < deadline:
            r3 = client.post(f"/projects/{second}/upload", content=_no_dxf_zip_bytes())
            last_status = r3.status_code
            if last_status == 202:
                break
            time.sleep(0.05)
        assert last_status == 202
