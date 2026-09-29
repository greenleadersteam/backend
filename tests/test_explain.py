from shapely.geometry import Point

from greenplan.explain.builder import build_explanations
from greenplan.layout.rules import PlantingRule, PlantingRuleSet
from greenplan.model import PlantingPoint


def test_build_explanations_short_shape():
    rules = PlantingRuleSet(
        source="test",
        rules=[
            PlantingRule(
                id="TREE_ROW_CURB", plant_type="tree", placement="row_along_curb",
                name_ru="Рядовая посадка", spacing_m=6.0, offset_m=2.2,
            )
        ],
    )
    points = [
        PlantingPoint(id="TREE_ROW_CURB-00001", geometry=Point(1.234, 5.678), plant_type="tree", rule_id="TREE_ROW_CURB"),
    ]

    explanations = build_explanations(points, rules)

    assert explanations == [
        {
            "id": "TREE_ROW_CURB-00001",
            "plant_type": "tree",
            "kind": "auto",
            "rule_id": "TREE_ROW_CURB",
            "rule_name_ru": "Рядовая посадка",
            "x": 1.23,
            "y": 5.68,
            "moved": False,
        }
    ]


def test_build_explanations_handles_unknown_rule_id():
    points = [PlantingPoint(id="X-1", geometry=Point(0, 0), plant_type="tree", rule_id="MISSING")]
    explanations = build_explanations(points, PlantingRuleSet(source="test", rules=[]))
    assert explanations[0]["rule_name_ru"] is None


def test_build_explanations_marks_moved_retyped_and_manual_points():
    rules = PlantingRuleSet(source="test", rules=[])
    original = {
        "A": PlantingPoint(id="A", geometry=Point(0, 0), plant_type="tree", rule_id="R"),
        "B": PlantingPoint(id="B", geometry=Point(10, 0), plant_type="tree", rule_id="R"),
        "C": PlantingPoint(id="C", geometry=Point(20, 0), plant_type="tree", rule_id="R"),
    }
    points = [
        PlantingPoint(id="A", geometry=Point(3, 4), plant_type="tree", rule_id="R"),
        PlantingPoint(id="B", geometry=Point(10, 0.001), plant_type="shrub", rule_id="R"),
        PlantingPoint(id="C", geometry=Point(20, 0), plant_type="tree", rule_id="R"),
        PlantingPoint(id="manual-00001", geometry=Point(5, 5), plant_type="shrub", rule_id=None, kind="manual"),
    ]

    by_id = {
        e["id"]: e
        for e in build_explanations(
            points, rules, original_points=original, added_in_version={"manual-00001": 2}
        )
    }

    assert by_id["A"]["moved"] is True
    assert by_id["A"]["displacement_m"] == 5.0
    assert by_id["A"]["zone_check"] == "not_checked"
    assert by_id["B"]["moved"] is False  # 1mm is round-trip noise
    assert by_id["B"]["original_plant_type"] == "tree"
    assert "note" in by_id["B"]
    assert by_id["C"] == {**by_id["C"], "moved": False} and "note" not in by_id["C"]
    manual = by_id["manual-00001"]
    assert manual["kind"] == "manual"
    assert manual["rule_id"] is None
    assert manual["added_in_version"] == 2
    assert manual["zone_check"] == "not_checked"
