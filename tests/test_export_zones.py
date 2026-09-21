from shapely.geometry import Polygon

from greenplan.export.zones import zoning_result_to_geojson
from greenplan.model import ZoningResult


def _zoning_with_extents():
    site_boundary = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    lawn_raw = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    base_area = site_boundary.intersection(lawn_raw)
    return ZoningResult(
        plant_types=["tree"],
        lawn_raw=lawn_raw,
        site_boundary=site_boundary,
        base_area=base_area,
        used_site_boundary=True,
        allowed={"tree": base_area},
        prohibited=[],
        uncovered_categories=[],
    )


def test_extent_features_present_and_tagged():
    geojson = zoning_result_to_geojson(_zoning_with_extents())
    zone_types = {f["properties"]["zone_type"] for f in geojson["features"]}
    assert {"site_boundary", "lawn_raw", "base_area", "allowed"} <= zone_types


def test_extent_features_have_no_plant_type_property():
    geojson = zoning_result_to_geojson(_zoning_with_extents())
    extent_features = [
        f for f in geojson["features"] if f["properties"]["zone_type"] in ("site_boundary", "lawn_raw", "base_area")
    ]
    assert len(extent_features) == 3
    for f in extent_features:
        assert "plant_type" not in f["properties"]


def test_missing_site_boundary_is_omitted_not_crashed():
    zoning = _zoning_with_extents()
    zoning.site_boundary = None
    geojson = zoning_result_to_geojson(zoning)
    zone_types = [f["properties"]["zone_type"] for f in geojson["features"]]
    assert "site_boundary" not in zone_types
    assert "lawn_raw" in zone_types
