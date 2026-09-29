from itertools import combinations

import pytest
from shapely.geometry import LineString, Polygon

from greenplan.layout.engine import generate_layout
from greenplan.layout.rules import PlantingRule, PlantingRuleSet
from greenplan.model import Feature, FeatureCollection, ZoningResult


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


def _square_zoning(plant_types=("tree", "shrub")):
    square = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)])
    return ZoningResult(
        plant_types=list(plant_types),
        lawn_raw=square,
        site_boundary=square,
        base_area=square,
        used_site_boundary=True,
        allowed={pt: square for pt in plant_types},
        prohibited=[],
        uncovered_categories=[],
    )


def test_grid_fill_respects_spacing_and_stays_inside_allowed_area():
    zoning = _square_zoning(("tree",))
    fc = FeatureCollection(root_file="t", source_folder=".", insunits_code=6, scale_to_meters=1.0, features=[])
    rules = PlantingRuleSet(
        source="test",
        rules=[PlantingRule(id="FILL", plant_type="tree", placement="grid_fill", name_ru="fill", spacing_m=5.0)],
    )

    points = generate_layout(zoning, fc, rules)
    assert len(points) > 10  # a 30x30 square at 5m spacing should yield a real grid

    for p in points:
        assert zoning.allowed["tree"].contains(p.geometry) or zoning.allowed["tree"].touches(p.geometry)
        assert p.rule_id == "FILL"
        assert p.plant_type == "tree"

    for a, b in combinations(points, 2):
        assert a.geometry.distance(b.geometry) >= 5.0 - 1e-6


def test_verbose_mode_does_not_change_results():
    zoning = _square_zoning(("tree",))
    fc = FeatureCollection(root_file="t", source_folder=".", insunits_code=6, scale_to_meters=1.0, features=[])
    rules = PlantingRuleSet(
        source="test",
        rules=[PlantingRule(id="FILL", plant_type="tree", placement="grid_fill", name_ru="fill", spacing_m=5.0)],
    )

    quiet = generate_layout(zoning, fc, rules, verbose=False)
    loud = generate_layout(zoning, fc, rules, verbose=True)
    assert len(quiet) == len(loud)
    assert {p.id for p in quiet} == {p.id for p in loud}


def test_row_along_curb_places_points_near_curb_offset():
    zoning = _square_zoning(("tree",))
    curb = LineString([(5, 15), (25, 15)])
    fc = FeatureCollection(
        root_file="t", source_folder=".", insunits_code=6, scale_to_meters=1.0,
        features=[_feature(curb, "road_edge")],
    )
    rules = PlantingRuleSet(
        source="test",
        rules=[
            PlantingRule(
                id="ROW", plant_type="tree", placement="row_along_curb", name_ru="row",
                spacing_m=6.0, offset_m=2.0,
            )
        ],
    )

    points = generate_layout(zoning, fc, rules)
    assert len(points) > 0
    for p in points:
        dist_from_curb = curb.distance(p.geometry)
        assert dist_from_curb == pytest.approx(2.0, abs=0.05)
        assert zoning.allowed["tree"].contains(p.geometry)


def test_unknown_plant_type_rule_is_skipped_not_fatal():
    zoning = _square_zoning(("tree",))  # no "shrub" in allowed
    fc = FeatureCollection(root_file="t", source_folder=".", insunits_code=6, scale_to_meters=1.0, features=[])
    rules = PlantingRuleSet(
        source="test",
        rules=[PlantingRule(id="X", plant_type="shrub", placement="grid_fill", name_ru="x", spacing_m=5.0)],
    )
    assert generate_layout(zoning, fc, rules) == []


def test_row_offset_clears_the_kerbs_street_category_setback():
    from greenplan.layout.engine import _row_offset
    from greenplan.layout.rules import PlantingRule
    from greenplan.pipeline import load_default_norms

    norms = load_default_norms()
    row = PlantingRule(id="R", plant_type="tree", placement="row_along_curb", name_ru="r", spacing_m=6, offset_m=2.2)
    assert _row_offset(row, None, norms) == pytest.approx(2.2)
    assert _row_offset(row, "arterial_citywide", norms) == pytest.approx(7.2)
    assert _row_offset(row, "arterial_citywide", None) == pytest.approx(2.2)
