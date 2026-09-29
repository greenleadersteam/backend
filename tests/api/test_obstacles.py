import io
import json
import uuid
import zipfile

import ezdxf
import pytest
from fastapi.testclient import TestClient

from greenplan.api import obstacles
from greenplan.georeference import geobridge
from greenplan.norms.schema import NormsTable, SetbackRule
from tests.api.test_routes import (  # noqa: F401 -- _mock_geobridge is an autouse fixture
    GEODETIC_POINT_LABELS,
    _create_project,
    _make_app,
    _mock_geobridge,
    _wait_for_terminal,
)

LOCAL_CRS = "local drawing coordinates, no geo-reference available"


@pytest.fixture
def norms():
    return NormsTable(
        source="test",
        rules=[
            SetbackRule(
                obstacle_category="underground_utilities", obstacle_subtype="gas",
                tree_m=1.5, shrub_m=1.5, citation="c",
            )
        ],
    )


def _feature(geometry, category, subtype=None, **extra):
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "rule_id": "3", "category": category, "subtype": subtype, "status": "auto",
            "layer": "L", "dxftype": "LINE", "source_file": "/srv/data/raw/a.dxf", "handle": "1F", **extra,
        },
    }


def _parsed(features, crs=LOCAL_CRS):
    return {
        "type": "FeatureCollection",
        "metadata": {
            "root_file": "/srv/data/raw/a.dxf", "source_folder": "/srv/data/raw",
            "source_insunits": 6, "scale_to_meters": 1.0, "crs": crs,
        },
        "features": features,
    }


def _line(x0, y0, x1, y1):
    return {"type": "LineString", "coordinates": [[x0, y0], [x1, y1]]}


def test_keeps_only_normed_categories_and_site_boundary(norms):
    result = obstacles.obstacles_geojson(
        _parsed([
            _feature(_line(0, 0, 1, 1), "underground_utilities", "gas"),
            _feature(_line(0, 0, 1, 1), "underground_utilities", "heat"),  # no norm for this subtype
            _feature(_line(0, 0, 1, 1), "contours", elevation=150.0),
            _feature(_line(0, 0, 1, 1), "site_boundary"),
        ]),
        norms,
    )
    assert [(f["properties"]["category"], f["properties"]["subtype"]) for f in result["features"]] == [
        ("underground_utilities", "gas"), ("site_boundary", None),
    ]


def test_strips_server_paths(norms):
    result = obstacles.obstacles_geojson(_parsed([_feature(_line(0, 0, 1, 1), "underground_utilities", "gas")]), norms)
    assert result["metadata"] == {"source_insunits": 6, "scale_to_meters": 1.0, "crs": LOCAL_CRS}
    props = result["features"][0]["properties"]
    assert "source_file" not in props
    assert props["handle"] == "1F" and props["layer"] == "L"


def test_splits_geometry_collections_and_multipoints(norms):
    collection = {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
            _line(0, 0, 5, 5),
            {"type": "MultiPoint", "coordinates": [[2, 2], [3, 3]]},
        ],
    }
    result = obstacles.obstacles_geojson(_parsed([_feature(collection, "underground_utilities", "gas")]), norms)
    assert [f["geometry"]["type"] for f in result["features"]] == ["Polygon", "LineString", "Point", "Point"]


def test_clips_to_extent_plus_margin_and_rounds(norms):
    parsed = _parsed([
        _feature(_line(0.123456, 0, 10, 10), "underground_utilities", "gas"),
        _feature(_line(150, 150, 160, 160), "underground_utilities", "gas"),  # 50 m out: kept
        _feature(_line(300, 300, 310, 310), "underground_utilities", "gas"),  # far: dropped
    ])
    result = obstacles.obstacles_geojson(parsed, norms, extent=(0, 0, 100, 100))
    assert len(result["features"]) == 2
    assert result["features"][0]["geometry"]["coordinates"][0] == [0.12, 0]
    # Idempotent: clipping the output again with the same extent changes nothing.
    assert obstacles.obstacles_geojson(result, norms, extent=(0, 0, 100, 100)) == result


def test_margin_is_converted_to_degrees_for_wgs84(norms):
    crs = "EPSG:4326 (WGS84 lon/lat)"
    near = 37.6 + 50 / (111_320 * 0.5625)  # ~50 m east at lat 55.75 (cos ~ 0.5625)
    parsed = _parsed([
        _feature(_line(near, 55.75, near, 55.75), "underground_utilities", "gas"),
        _feature(_line(37.61, 55.75, 37.61, 55.75), "underground_utilities", "gas"),  # ~600 m east
    ], crs=crs)
    result = obstacles.obstacles_geojson(parsed, norms, extent=(37.59, 55.74, 37.6, 55.75))
    assert len(result["features"]) == 1


def test_ensure_obstacles_builds_lazily_from_parsed_and_zones(tmp_path):
    parsed = _parsed([
        _feature(_line(0, 0, 10, 10), "underground_utilities", "gas"),
        _feature(_line(5000, 5000, 5010, 5010), "underground_utilities", "gas"),
    ])
    zones = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [20, 0], [20, 20], [0, 0]]]},
        "properties": {"zone_type": "base_area"},
    }]}
    (tmp_path / "parsed.geojson").write_text(json.dumps(parsed), encoding="utf-8")
    (tmp_path / "zones.geojson").write_text(json.dumps(zones), encoding="utf-8")

    path = obstacles.ensure_obstacles(tmp_path)
    assert path == tmp_path / "obstacles.geojson"
    assert len(json.loads(path.read_text(encoding="utf-8"))["features"]) == 1


def test_ensure_obstacles_none_without_parsed(tmp_path):
    assert obstacles.ensure_obstacles(tmp_path) is None


# -- route --


def _dxf_with_gas_pipe_zip_bytes(tmp_path) -> bytes:
    dxf_path = tmp_path / f"root-{uuid.uuid4().hex}.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for i, label in enumerate(GEODETIC_POINT_LABELS):
        msp.add_text(label, dxfattribs={"layer": "Геодезические пункты", "insert": (i * 100.0, 0.0)})
    msp.add_line((0, 10), (100, 10), dxfattribs={"layer": "Газопровод"})
    doc.saveas(dxf_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write(dxf_path, "root.dxf")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(_make_app(tmp_path)) as c:
        yield c


def test_obstacles_route_for_ready_project(client, tmp_path):
    pid = _create_project(client).json()["id"]
    client.post(f"/projects/{pid}/upload", content=_dxf_with_gas_pipe_zip_bytes(tmp_path))
    assert _wait_for_terminal(client, pid)["status"] == "ready"

    r = client.get(f"/projects/{pid}/obstacles")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/geo+json")
    body = r.json()
    assert body["metadata"]["crs"] == "EPSG:4326 (WGS84 lon/lat)"
    assert "root_file" not in body["metadata"]
    [pipe] = body["features"]
    assert (pipe["properties"]["category"], pipe["properties"]["subtype"]) == ("underground_utilities", "gas")
    assert "source_file" not in pipe["properties"]
    lon, lat = pipe["geometry"]["coordinates"][0]
    assert 37 < lon < 38 and 55 < lat < 56


def test_obstacles_route_after_georeferencing_failure_is_in_drawing_frame(client, tmp_path, monkeypatch):
    monkeypatch.setattr(geobridge, "fetch_points", lambda label, **kwargs: [])
    pid = _create_project(client).json()["id"]
    client.post(f"/projects/{pid}/upload", content=_dxf_with_gas_pipe_zip_bytes(tmp_path))
    data = _wait_for_terminal(client, pid)
    assert data["job"]["error"]["code"] == "insufficient_geodetic_points"

    r = client.get(f"/projects/{pid}/obstacles")
    assert r.status_code == 200
    body = r.json()
    assert body["metadata"]["crs"] == LOCAL_CRS
    assert body["features"][0]["geometry"]["coordinates"] == [[0, 10], [100, 10]]


def test_obstacles_route_404_before_processing_and_before_parsing(client):
    pid = _create_project(client).json()["id"]
    assert client.get(f"/projects/{pid}/obstacles").status_code == 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no dxf")
    client.post(f"/projects/{pid}/upload", content=buf.getvalue())
    assert _wait_for_terminal(client, pid)["status"] == "failed"
    assert client.get(f"/projects/{pid}/obstacles").status_code == 404
    assert client.get("/projects/nope/obstacles").status_code == 404
