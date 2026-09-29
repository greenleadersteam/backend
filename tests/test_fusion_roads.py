import pytest
from shapely.geometry import LineString, MultiLineString, box

from greenplan.fusion.roads import RoadSegment, fuse_roads, resolve_category
from greenplan.model import Feature, FeatureCollection

EXTENT = box(0, -20, 100, 40)


def _f(geom, category, subtype=None):
    return Feature(
        geometry=geom, category=category, subtype=subtype, rule_id="x", status="auto",
        layer="L", dxftype="LWPOLYLINE", source_file="plan.dxf",
    )


def _fc(*features):
    return FeatureCollection(
        root_file="plan.dxf", source_folder=".", insunits_code=6, scale_to_meters=1.0, features=list(features)
    )


def _dashed(y, x0=0.0, x1=100.0, dash=0.7, gap=0.5):
    parts, x = [], x0
    while x < x1:
        parts.append(LineString([(x, y), (min(x + dash, x1), y)]))
        x += dash + gap
    return MultiLineString(parts)


def _by_category(fc, category):
    return [f for f in fc.features if f.category == category]


@pytest.mark.parametrize("hint, overture, expected", [
    (None, None, None),
    (None, "local", "local"),
    ("local", "arterial_citywide", "local"),
    ("arterial", "arterial_citywide", "arterial_citywide"),
    ("arterial", "local", "arterial_district"),
    ("arterial", None, "arterial"),
])
def test_resolve_category(hint, overture, expected):
    assert resolve_category(hint, overture) == expected


def test_plan_carriageway_category_from_hint_and_overture():
    street = box(0, 0, 100, 10)
    hint = _f(box(10, 0, 40, 10), "carriageway", "arterial")
    plain = _f(street, "carriageway")
    primary = RoadSegment("s", LineString([(0, 5), (100, 5)]), "primary")
    fused, _ = fuse_roads(_fc(plain, hint), [primary], EXTENT)
    subtypes = {f.geometry.area: f.subtype for f in _by_category(fused, "carriageway")}
    assert subtypes == {1000.0: "arterial_citywide", 300.0: "arterial_citywide"}

    fused, _ = fuse_roads(_fc(plain, hint), [], EXTENT)
    assert {f.subtype for f in _by_category(fused, "carriageway")} == {"arterial"}


def test_kerb_bounded_face_from_dashed_kerbs_and_kerb_classification():
    kerbs = [_f(_dashed(0), "road_edge"), _f(_dashed(10), "road_edge"), _f(_dashed(25), "road_edge")]
    road = RoadSegment("r", LineString([(0, 5), (100, 5)]), "residential")
    fused, report = fuse_roads(_fc(*kerbs), [road], EXTENT)

    [cw] = _by_category(fused, "carriageway")
    assert cw.status == "fused_kerb_bounded"
    assert cw.subtype == "local"
    assert cw.geometry.area == pytest.approx(1000, rel=0.05)

    edge = [f for f in _by_category(fused, "road_edge") if f.extra.get("kerb_side") == "carriageway"]
    assert {f.subtype for f in edge} == {"local"}
    sidewalk = _by_category(fused, "footpath_edge")
    assert len(sidewalk) == 1 and sidewalk[0].geometry.bounds[1] == pytest.approx(25)
    assert report.kerb_m_sidewalk == pytest.approx(sidewalk[0].geometry.length)


def test_no_kerbs_falls_back_to_default_width():
    road = RoadSegment("s", LineString([(0, 5), (100, 5)]), "service")
    fused, report = fuse_roads(_fc(), [road], EXTENT)
    [cw] = _by_category(fused, "carriageway")
    assert cw.status == "fused_default_width"
    assert cw.subtype == "driveway"
    assert cw.geometry.area == pytest.approx(100 * 2 * 2.75, rel=0.05)
    assert report.fused_default_width == 1


def test_known_surfaces_win_over_fused_carriageway():
    lawn = _f(box(0, -20, 100, 40), "green_existing", "lawn")
    road = RoadSegment("s", LineString([(0, 5), (100, 5)]), "service")
    fused, _ = fuse_roads(_fc(lawn), [road], EXTENT)
    assert _by_category(fused, "carriageway") == []


def test_footways_and_off_extent_kerbs_are_ignored():
    far_kerb = _f(LineString([(500, 0), (600, 0)]), "road_edge")
    footway = RoadSegment("f", LineString([(0, 5), (100, 5)]), "footway")
    fused, report = fuse_roads(_fc(far_kerb), [footway], EXTENT)
    assert report.vehicle_segments == 0
    assert _by_category(fused, "carriageway") == []
    assert _by_category(fused, "road_edge") == [far_kerb]
