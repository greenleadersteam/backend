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
            "rule_id": "TREE_ROW_CURB",
            "rule_name_ru": "Рядовая посадка",
            "x": 1.23,
            "y": 5.68,
        }
    ]


def test_build_explanations_handles_unknown_rule_id():
    points = [PlantingPoint(id="X-1", geometry=Point(0, 0), plant_type="tree", rule_id="MISSING")]
    explanations = build_explanations(points, PlantingRuleSet(source="test", rules=[]))
    assert explanations[0]["rule_name_ru"] is None
