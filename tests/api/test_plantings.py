import shutil
import threading
import time
from pathlib import Path

import ezdxf
import pytest
from fastapi.testclient import TestClient

from greenplan.api import plantings
from greenplan.api.plantings import EditValidationError, PlantingEdit, _apply_edit
from tests.api.test_routes import (  # noqa: F401 -- _mock_geobridge is an autouse fixture
    _create_project,
    _empty_dxf_zip_bytes,
    _make_app,
    _mock_geobridge,
    _wait_for_terminal,
)

INSIDE = {"lon": 37.61, "lat": 55.705}


def _auto_geojson() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [37.6, 55.7]},
                "properties": {"id": f"R-0000{i}", "plant_type": "tree", "kind": "auto", "rule_id": "R"},
            }
            for i in (1, 2, 3)
        ],
    }


BBOX = (37.0, 55.0, 38.0, 56.0)


def test_apply_edit_add_update_delete():
    edit = PlantingEdit(
        add=[{"client_id": "c1", "plant_type": "shrub", **INSIDE}],
        update=[{"id": "R-00001", "lon": 37.7, "lat": 55.8}, {"id": "R-00002", "plant_type": "shrub"}],
        delete=["R-00003"],
    )
    new, id_map, next_manual = _apply_edit(_auto_geojson(), edit, 2, 7, BBOX)

    assert id_map == {"c1": "manual-00007"}
    assert next_manual == 8
    by_id = {f["properties"]["id"]: f for f in new["features"]}
    assert set(by_id) == {"R-00001", "R-00002", "manual-00007"}
    assert by_id["R-00001"]["geometry"]["coordinates"] == [37.7, 55.8]
    assert by_id["R-00001"]["properties"]["kind"] == "auto"
    assert by_id["R-00002"]["properties"]["plant_type"] == "shrub"
    assert by_id["R-00002"]["geometry"]["coordinates"] == [37.6, 55.7]
    manual = by_id["manual-00007"]["properties"]
    assert manual == {
        "id": "manual-00007", "plant_type": "shrub", "kind": "manual", "rule_id": None, "added_in_version": 2,
    }


@pytest.mark.parametrize(
    "edit, error_type, loc",
    [
        ({}, "empty_edit", ["body"]),
        ({"update": [{"id": "nope", "lon": 37.6, "lat": 55.7}]}, "unknown_id", ["body", "update", 0, "id"]),
        ({"delete": ["nope"]}, "unknown_id", ["body", "delete", 0]),
        (
            {"update": [{"id": "R-00001", "lon": 37.6, "lat": 55.7}], "delete": ["R-00001"]},
            "update_delete_conflict", ["body", "delete", 0],
        ),
        (
            {"add": [{"client_id": "c", "plant_type": "tree", **INSIDE}] * 2},
            "duplicate_client_id", ["body", "add", 1, "client_id"],
        ),
        (
            {"update": [{"id": "R-00001", "plant_type": "shrub"}] * 2},
            "duplicate_id", ["body", "update", 1, "id"],
        ),
        (
            {"add": [{"client_id": "c", "plant_type": "tree", "lon": 10.0, "lat": 10.0}]},
            "outside_project_bbox", ["body", "add", 0],
        ),
        ({"update": [{"id": "R-00001", "lon": 37.6}]}, "lon_lat_pair", ["body", "update", 0]),
        ({"update": [{"id": "R-00001"}]}, "nothing_to_update", ["body", "update", 0]),
        (
            {"add": [{"client_id": "c", "plant_type": "tree", "lon": float("nan"), "lat": 55.7}]},
            "coordinates_not_finite", ["body", "add", 0],
        ),
    ],
)
def test_apply_edit_rejects_invalid_edits(edit, error_type, loc):
    with pytest.raises(EditValidationError) as exc_info:
        _apply_edit(_auto_geojson(), PlantingEdit(**edit), 2, 1, BBOX)
    assert {"loc": loc, "type": error_type} in [
        {"loc": e["loc"], "type": e["type"]} for e in exc_info.value.errors
    ]


def _ready_project(client, tmp_path) -> str:
    pid = _create_project(client).json()["id"]
    client.post(f"/projects/{pid}/upload", content=_empty_dxf_zip_bytes(tmp_path))
    assert _wait_for_terminal(client, pid)["status"] == "ready"
    return pid


def _wait_for_export(client, pid, version_id, timeout=10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        versions = {v["id"]: v for v in client.get(f"/projects/{pid}/plantings").json()}
        if versions[version_id]["export_status"] in ("ready", "failed"):
            return versions[version_id]
        time.sleep(0.05)
    pytest.fail("export did not finish in time")


def _add(client, pid, base, client_id="c1", plant_type="tree", name=None):
    body = {"add": [{"client_id": client_id, "plant_type": plant_type, **INSIDE}]}
    if name is not None:
        body["name"] = name
    return client.post(f"/projects/{pid}/plantings/{base}/edit", json=body)


@pytest.fixture
def client(tmp_path):
    with TestClient(_make_app(tmp_path)) as c:
        yield c


def _processed_dir(tmp_path, pid) -> Path:
    return tmp_path / "data" / "projects" / pid / "processed"


def test_first_version_created_by_processing(client, tmp_path):
    pid = _ready_project(client, tmp_path)

    versions = client.get(f"/projects/{pid}/plantings").json()
    assert len(versions) == 1
    v1 = versions[0]
    assert v1["id"] == 1 and v1["kind"] == "auto" and v1["based_on"] is None
    assert v1["export_status"] == "ready"
    assert client.get(f"/projects/{pid}/plantings/1").json()["type"] == "FeatureCollection"
    assert client.get(f"/projects/{pid}/plantings/1/dxf").status_code == 200
    assert client.get(f"/projects/{pid}/plantings/1/explanation").json() == []
    assert client.get(f"/projects/{pid}/plantings/2").status_code == 404


def test_edit_creates_version_and_exports_it(client, tmp_path):
    pid = _ready_project(client, tmp_path)

    r = _add(client, pid, 1, name="Правка 1")
    assert r.status_code == 201
    body = r.json()
    assert body["id_map"] == {"c1": "manual-00001"}
    version = body["version"]
    assert version["id"] == 2 and version["name"] == "Правка 1" and version["kind"] == "manual"
    assert version["based_on"] == 1
    assert version["counts"] == {"tree": 1, "shrub": 0, "total": 1}

    assert _wait_for_export(client, pid, 2)["export_status"] == "ready"

    # Unversioned endpoints serve the latest version.
    planting = client.get(f"/projects/{pid}/planting").json()
    assert [f["properties"]["id"] for f in planting["features"]] == ["manual-00001"]
    explanation = client.get(f"/projects/{pid}/explanation").json()
    assert explanation[0]["kind"] == "manual" and explanation[0]["added_in_version"] == 2

    dxf = client.get(f"/projects/{pid}/dxf")
    assert dxf.status_code == 200
    dxf_path = tmp_path / "v2.dxf"
    dxf_path.write_bytes(dxf.content)
    circles = [e for e in ezdxf.readfile(str(dxf_path)).modelspace() if e.dxftype() == "CIRCLE"]
    assert len(circles) == 1
    assert [value for _, value in circles[0].get_xdata("GREENPLAN")] == ["tree", "", "manual-00001", "manual"]


def test_manual_ids_never_reused_and_history_is_linear(client, tmp_path):
    pid = _ready_project(client, tmp_path)

    _add(client, pid, 1)  # v2: manual-00001
    r3 = client.post(f"/projects/{pid}/plantings/2/edit", json={"delete": ["manual-00001"]})
    assert r3.json()["version"]["counts"]["total"] == 0
    r4 = _add(client, pid, 3)
    assert r4.json()["id_map"] == {"c1": "manual-00002"}

    # Editing an older version appends, based on it.
    r5 = _add(client, pid, 2, client_id="x")
    assert r5.json()["version"]["id"] == 5
    assert r5.json()["version"]["based_on"] == 2
    ids = [f["properties"]["id"] for f in client.get(f"/projects/{pid}/plantings/5").json()["features"]]
    assert ids == ["manual-00001", "manual-00003"]
    assert [v["id"] for v in client.get(f"/projects/{pid}/plantings").json()] == [1, 2, 3, 4, 5]


def test_edit_validation_errors_are_422(client, tmp_path):
    pid = _ready_project(client, tmp_path)

    # Business-rule and schema errors share FastAPI's {loc, msg, type} shape.
    unknown = client.post(f"/projects/{pid}/plantings/1/edit", json={"delete": ["nope"]})
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == [
        {"loc": ["body", "delete", 0], "msg": "no point with id 'nope' in this version", "type": "unknown_id"}
    ]
    bad_type = {"add": [{"client_id": "c", "plant_type": "flower", **INSIDE}]}
    schema_error = client.post(f"/projects/{pid}/plantings/1/edit", json=bad_type)
    assert schema_error.status_code == 422
    assert schema_error.json()["detail"][0]["loc"] == ["body", "add", 0, "plant_type"]
    assert client.post(f"/projects/{pid}/plantings/9/edit", json={"delete": ["x"]}).status_code == 404
    assert len(client.get(f"/projects/{pid}/plantings").json()) == 1


def test_legacy_project_gets_first_version_but_no_edited_export(client, tmp_path):
    pid = _ready_project(client, tmp_path)
    processed = _processed_dir(tmp_path, pid)
    shutil.rmtree(processed / "plantings")
    (processed / "export_context.yaml").unlink()

    versions = client.get(f"/projects/{pid}/plantings").json()
    assert [v["id"] for v in versions] == [1]
    assert client.get(f"/projects/{pid}/dxf").status_code == 200

    r = _add(client, pid, 1)
    assert r.json()["version"]["export_status"] == "none"
    conflict = client.get(f"/projects/{pid}/plantings/2/dxf")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "transform_unavailable"


def test_export_waits_for_a_free_slot(tmp_path):
    release = threading.Event()

    def blocking_export(version_dir):
        release.wait(timeout=5)
        (version_dir / plantings.DXF_FILENAME).write_text("dxf")
        (version_dir / plantings.EXPLANATION_FILENAME).write_text("[]")
        plantings.set_export_status(version_dir, plantings.EXPORT_READY)

    with TestClient(_make_app(tmp_path, max_concurrent_jobs=1)) as client:
        pid = _ready_project(client, tmp_path)
        client.app.state.jobs._export_fn = blocking_export

        assert _add(client, pid, 1).json()["version"]["export_status"] == "pending"  # takes the only slot
        assert _add(client, pid, 2).json()["version"]["export_status"] == "none"

        pending = client.get(f"/projects/{pid}/plantings/2/dxf")
        assert pending.status_code == 202
        assert pending.json() == {"version": 2, "export_status": "pending"}
        assert client.get(f"/projects/{pid}/plantings/3/dxf").status_code == 429

        release.set()
        _wait_for_export(client, pid, 2)
        deadline = time.time() + 5
        while client.get(f"/projects/{pid}/plantings/3/explanation").status_code != 202:
            assert time.time() < deadline
            time.sleep(0.05)
        _wait_for_export(client, pid, 3)
        assert client.get(f"/projects/{pid}/plantings/3/dxf").status_code == 200


def test_failed_export_is_reported(tmp_path):
    def failing_export(version_dir):
        raise RuntimeError("boom")  # worker died before recording anything

    with TestClient(_make_app(tmp_path)) as client:
        pid = _ready_project(client, tmp_path)
        client.app.state.jobs._export_fn = failing_export
        _add(client, pid, 1)
        assert _wait_for_export(client, pid, 2)["export_status"] == "failed"
        r = client.get(f"/projects/{pid}/plantings/2/dxf")
        assert r.status_code == 500
        assert r.json()["detail"]["code"] == "export_failed"


def test_reconcile_resets_pending_exports(tmp_path):
    vdir = tmp_path / "p1" / "processed" / "plantings" / "2"
    vdir.mkdir(parents=True)
    plantings.write_version_metadata(
        vdir,
        plantings.PlantingVersion(
            id=2, name="v", kind="manual", created_at="2026-01-01T00:00:00Z", based_on=1,
            counts={"tree": 0, "shrub": 0, "total": 0}, export_status="pending",
        ),
    )
    plantings.reconcile_interrupted_exports(tmp_path)
    assert plantings.read_version_metadata(vdir).export_status == "none"


def test_moved_auto_point_displacement_in_meters(client, tmp_path):
    """Real export path: WGS84 -> UTM -> drawing frame, displacement vs v1."""
    import json
    import math

    pid = _ready_project(client, tmp_path)
    v1_path = _processed_dir(tmp_path, pid) / "plantings" / "1" / "planting.geojson"
    v1 = json.loads(v1_path.read_text())
    v1["features"] = _auto_geojson()["features"][:1]
    v1_path.write_text(json.dumps(v1))

    r = client.post(
        f"/projects/{pid}/plantings/1/edit",
        json={"update": [{"id": "R-00001", "lon": 37.6001, "lat": 55.7}]},
    )
    assert r.status_code == 201
    assert _wait_for_export(client, pid, 2)["export_status"] == "ready"

    [entry] = client.get(f"/projects/{pid}/plantings/2/explanation").json()
    expected_m = 0.0001 * 111_320 * math.cos(math.radians(55.7))
    assert entry["kind"] == "auto" and entry["moved"] is True
    assert entry["displacement_m"] == pytest.approx(expected_m, abs=0.1)


def test_openapi_documents_media_types_and_export_statuses(client):
    """The frontend generates its types from /openapi.json -- keep it accurate."""
    paths = client.get("/openapi.json").json()["paths"]

    def content_types(path, method, status):
        return set(paths[path][method]["responses"][status].get("content", {}))

    for path in ("/projects/{project_id}/planting", "/projects/{project_id}/plantings/{version_id}"):
        assert content_types(path, "get", "200") == {"application/geo+json"}
    for path in ("/projects/{project_id}/dxf", "/projects/{project_id}/plantings/{version_id}/dxf"):
        assert content_types(path, "get", "200") == {"image/vnd.dxf"}
    for path in (
        "/projects/{project_id}/dxf",
        "/projects/{project_id}/plantings/{version_id}/dxf",
        "/projects/{project_id}/explanation",
        "/projects/{project_id}/plantings/{version_id}/explanation",
    ):
        for status in ("202", "404", "409", "429", "500"):
            assert content_types(path, "get", status) == {"application/json"}, (path, status)
    assert content_types("/projects/{project_id}/plantings/{version_id}/edit", "post", "422") == {"application/json"}


def test_dxf_download_is_named_after_project_and_version(tmp_path):
    from urllib.parse import unquote

    with TestClient(_make_app(tmp_path)) as client:
        pid = _create_project(client, name='Сквер: "Северный"/2').json()["id"]
        client.post(f"/projects/{pid}/upload", content=_empty_dxf_zip_bytes(tmp_path))
        assert _wait_for_terminal(client, pid)["status"] == "ready"

        for url in (f"/projects/{pid}/dxf", f"/projects/{pid}/plantings/1/dxf"):
            r = client.get(url, headers={"Origin": "https://app.greenleaders.online"})
            assert r.status_code == 200
            assert r.headers["content-type"] == "image/vnd.dxf"
            disposition = r.headers["content-disposition"]
            assert disposition.startswith("attachment; filename*=utf-8''")
            assert unquote(disposition.split("''", 1)[1]) == "Сквер_ _Северный__2 — версия 1.dxf"
            assert "content-disposition" in r.headers["access-control-expose-headers"].lower()


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Сквер", "Сквер — версия 3.dxf"),
        ("  a\tb\nc  ", "a_b_c — версия 3.dxf"),
        ("...", "план посадок — версия 3.dxf"),
        ("x" * 300, "x" * 120 + " — версия 3.dxf"),
    ],
)
def test_dxf_filename_sanitizes_project_name(name, expected):
    from greenplan.api.app import _dxf_filename

    assert _dxf_filename(name, 3) == expected
