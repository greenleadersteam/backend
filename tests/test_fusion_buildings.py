from shapely.geometry import LineString, box

from greenplan.fusion.buildings import EducationArea, OvertureBuilding, fuse_buildings
from greenplan.model import Feature, FeatureCollection


def _plan(geom, category="buildings"):
    return Feature(
        geometry=geom, category=category, rule_id="4", status="auto",
        layer="Здания", dxftype="LWPOLYLINE", source_file="plan.dxf",
    )


def _fc(*features):
    return FeatureCollection(
        root_file="plan.dxf", source_folder=".", insunits_code=6, scale_to_meters=1.0, features=list(features)
    )


def _overture(features):
    return [f for f in features if f.extra.get("source") == "overture"]


def test_plan_polygon_kept_and_gets_overture_attributes():
    plan_poly = box(0, 0, 20, 20)
    ov = OvertureBuilding(id="o1", geometry=box(1, 1, 21, 21), cls="apartments", num_floors=9)
    fused, report = fuse_buildings(_fc(_plan(plan_poly)), [ov], [])

    kept = fused.features[0]
    assert kept.geometry.equals(plan_poly)
    assert kept.extra["source"] == "plan"
    assert kept.extra["overture_id"] == "o1"
    assert kept.extra["num_floors"] == 9
    assert _overture(fused.features) == []
    assert report.overture_redundant == 1


def test_footprint_backed_by_plan_lines_is_confirmed_and_lines_are_kept():
    walls = _plan(LineString([(0, 0), (20, 0), (20, 20)]))
    backed = OvertureBuilding(id="backed", geometry=box(0.5, 0.5, 20.5, 20.5))
    lonely = OvertureBuilding(id="lonely", geometry=box(100, 100, 120, 120))
    fused, report = fuse_buildings(_fc(walls), [backed, lonely], [])

    status = {f.extra["overture_id"]: f.status for f in _overture(fused.features)}
    assert status == {"backed": "overture_confirmed", "lonely": "overture_unconfirmed"}
    assert fused.features[0] == walls
    assert report.overture_confirmed == 1 and report.overture_unconfirmed == 1


def test_school_detection_by_class_and_by_land_use():
    school_ground = EducationArea(geometry=box(0, 0, 200, 200), cls="school")
    buildings = [
        OvertureBuilding(id="by_class", geometry=box(500, 500, 540, 540), cls="kindergarten"),
        OvertureBuilding(id="by_ground", geometry=box(10, 10, 50, 50)),
        OvertureBuilding(id="shed", geometry=box(100, 100, 108, 108)),
        OvertureBuilding(id="boiler", geometry=box(120, 120, 160, 160), cls="service"),
    ]
    fused, report = fuse_buildings(_fc(), buildings, [school_ground])

    subtype = {f.extra["overture_id"]: f.subtype for f in _overture(fused.features)}
    assert subtype == {
        "by_class": "school_kindergarten",
        "by_ground": "school_kindergarten",
        "shed": None,
        "boiler": None,
    }
    assert report.school_kindergarten == 2


def test_non_building_features_untouched():
    lawn = _plan(box(0, 0, 50, 50), category="green_existing")
    fused, _ = fuse_buildings(_fc(lawn), [OvertureBuilding(id="o", geometry=box(10, 10, 20, 20))], [])
    assert fused.features[0] == lawn
