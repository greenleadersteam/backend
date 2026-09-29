import pytest
from shapely.geometry import LineString, Polygon

from greenplan.model import Feature, FeatureCollection
from greenplan.norms.schema import NormsTable, SetbackRule
from greenplan.zoning.engine import compute_zones


def _feature(geometry, category, subtype=None) -> Feature:
    return Feature(
        geometry=geometry,
        category=category,
        subtype=subtype,
        rule_id="0",
        status="auto",
        layer="test",
        dxftype="TEST",
        source_file="test.dxf",
        handle=None,
    )


@pytest.fixture
def norms():
    return NormsTable(
        source="test",
        rules=[
            SetbackRule(
                obstacle_category="underground_utilities",
                obstacle_subtype="gas",
                tree_m=2.0,
                shrub_m=1.0,
                citation="test citation",
            )
        ],
    )


@pytest.fixture
def fc():
    lawn = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    gas_pipe = LineString([(10, -5), (10, 25)])
    building = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])  # no norms entry -> uncovered
    return FeatureCollection(
        root_file="test.dxf",
        source_folder=".",
        insunits_code=6,
        scale_to_meters=1.0,
        features=[
            _feature(lawn, "green_existing", "lawn"),
            _feature(lawn, "site_boundary"),
            _feature(gas_pipe, "underground_utilities", "gas"),
            _feature(building, "buildings"),
        ],
    )


def test_base_area_is_lawn_intersect_site_boundary(fc, norms):
    zoning = compute_zones(fc, norms)
    assert zoning.used_site_boundary
    assert zoning.base_area.area == pytest.approx(400.0, rel=1e-6)


def test_lawn_raw_and_site_boundary_are_exposed_separately(fc, norms):
    zoning = compute_zones(fc, norms)
    assert zoning.lawn_raw.area == pytest.approx(400.0, rel=1e-6)
    assert zoning.site_boundary.area == pytest.approx(400.0, rel=1e-6)


def test_verbose_mode_does_not_change_results(fc, norms):
    quiet = compute_zones(fc, norms, verbose=False)
    loud = compute_zones(fc, norms, verbose=True)
    assert quiet.base_area.area == pytest.approx(loud.base_area.area, rel=1e-9)
    assert quiet.allowed["tree"].area == pytest.approx(loud.allowed["tree"].area, rel=1e-9)
    assert len(quiet.prohibited) == len(loud.prohibited)


def test_allowed_area_shrinks_by_buffered_distance(fc, norms):
    zoning = compute_zones(fc, norms)
    # gas pipe buffered 2.0m removes an 4m-wide x 20m-tall band from the 20x20 lawn
    assert zoning.allowed["tree"].area == pytest.approx(400.0 - 4 * 20, rel=0.02)
    # shrub norm is 1.0m -> a 2m-wide band
    assert zoning.allowed["shrub"].area == pytest.approx(400.0 - 2 * 20, rel=0.02)


def test_prohibited_zones_carry_citation_and_reason(fc, norms):
    zoning = compute_zones(fc, norms)
    assert len(zoning.prohibited) == 2  # one per plant type
    for zone in zoning.prohibited:
        assert zone.obstacle_category == "underground_utilities"
        assert zone.obstacle_subtype == "gas"
        assert zone.citation == "test citation"
        assert "газ" in zone.reason.lower() or "gas" in zone.reason.lower()
        assert zone.norm_id == f"underground_utilities-gas-{zone.plant_type}"
        assert zone.basis == "regulation"


def test_service_default_shrub_zone_gets_its_own_citation(fc):
    from greenplan.pipeline import load_default_norms

    zoning = compute_zones(fc, load_default_norms())
    gas = {z.plant_type: z for z in zoning.prohibited if z.obstacle_subtype == "gas"}
    assert gas["tree"].norm_id == "743-pp-gas-tree"
    assert gas["tree"].clause.startswith("п. 3.6.3")
    assert "743-ПП" in gas["tree"].citation
    assert gas["shrub"].basis == "service_default"
    assert gas["shrub"].clause is None
    assert "743-ПП" not in gas["shrub"].citation


def test_uncovered_categories_flags_buildings_but_not_structural_ones(fc, norms):
    zoning = compute_zones(fc, norms)
    assert ("buildings", None) in zoning.uncovered_categories
    assert ("green_existing", "lawn") not in zoning.uncovered_categories
    assert ("site_boundary", None) not in zoning.uncovered_categories


def test_no_site_boundary_falls_back_to_full_lawn(norms):
    lawn = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    fc_no_boundary = FeatureCollection(
        root_file="test.dxf", source_folder=".", insunits_code=6, scale_to_meters=1.0,
        features=[_feature(lawn, "green_existing", "lawn")],
    )
    zoning = compute_zones(fc_no_boundary, norms)
    assert not zoning.used_site_boundary
    assert zoning.base_area.area == pytest.approx(400.0, rel=1e-6)
