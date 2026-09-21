"""Per-planting-point explanation records.

Deliberately short, per the user's request: just which declarative planting
rule placed the point. The detailed "why is this area allowed" justification
(setback distances, citations) lives once in the grouped prohibited-zones
GeoJSON (see greenplan.export.zones), not duplicated per point -- unlike the
prototype's full7_assemble.py, which recomputed and repeated a full
distance-to-every-obstacle breakdown for every single point.
"""

from __future__ import annotations

from greenplan.layout.rules import PlantingRuleSet
from greenplan.model import PlantingPoint


def build_explanations(points: list[PlantingPoint], planting_rules: PlantingRuleSet) -> list[dict]:
    rules_by_id = {rule.id: rule for rule in planting_rules.rules}
    explanations = []
    for pt in points:
        rule = rules_by_id.get(pt.rule_id)
        explanations.append(
            {
                "id": pt.id,
                "plant_type": pt.plant_type,
                "rule_id": pt.rule_id,
                "rule_name_ru": rule.name_ru if rule else None,
                "x": round(pt.geometry.x, 2),
                "y": round(pt.geometry.y, 2),
            }
        )
    return explanations
