"""Per-planting-point explanation records.

Deliberately short, per the user's request: just which declarative planting
rule placed the point. The detailed "why is this area allowed" justification
(setback distances, citations) lives once in the grouped prohibited-zones
GeoJSON (see greenplan.export.zones), not duplicated per point -- unlike the
prototype's full7_assemble.py, which recomputed and repeated a full
distance-to-every-obstacle breakdown for every single point.

For a manually edited planting version (greenplan.api.plantings), each point
also says whether a person changed it: an auto point that was moved or
retyped keeps its rule_id for provenance, but the rule no longer vouches for
its placement, and a manual point has no rule at all. Zone checks for edits
happen on the frontend only, so nothing here claims compliance for them.
"""

from __future__ import annotations

from greenplan.layout.rules import PlantingRuleSet
from greenplan.model import PlantingPoint

# Below this, a coordinate difference is WGS84 round-trip noise, not a move.
MOVE_THRESHOLD_M = 0.01

NOTE_MOVED = "Перемещено вручную; соответствие правилу размещения не гарантируется"
NOTE_RETYPED = "Тип растения изменён вручную; соответствие правилу размещения не гарантируется"
NOTE_MANUAL = "Добавлено вручную; проверка по зонам на сервере не выполнялась"


def build_explanations(
    points: list[PlantingPoint],
    planting_rules: PlantingRuleSet,
    *,
    original_points: dict[str, PlantingPoint] | None = None,
    added_in_version: dict[str, int] | None = None,
) -> list[dict]:
    """`original_points` are the automatic (version 1) points by id, in the
    same frame as `points`; without them every auto point counts as unmoved.
    `added_in_version` maps manual point ids to the version that added them.
    """
    rules_by_id = {rule.id: rule for rule in planting_rules.rules}
    original_points = original_points or {}
    added_in_version = added_in_version or {}
    explanations = []
    for pt in points:
        rule = rules_by_id.get(pt.rule_id) if pt.rule_id is not None else None
        entry = {
            "id": pt.id,
            "plant_type": pt.plant_type,
            "kind": pt.kind,
            "rule_id": pt.rule_id,
            "rule_name_ru": rule.name_ru if rule else None,
            "x": round(pt.geometry.x, 2),
            "y": round(pt.geometry.y, 2),
        }
        if pt.kind == "manual":
            entry["added_in_version"] = added_in_version.get(pt.id)
            entry["zone_check"] = "not_checked"
            entry["note"] = NOTE_MANUAL
        else:
            original = original_points.get(pt.id)
            displacement = pt.geometry.distance(original.geometry) if original is not None else 0.0
            moved = displacement >= MOVE_THRESHOLD_M
            retyped = original is not None and original.plant_type != pt.plant_type
            entry["moved"] = moved
            if moved:
                entry["displacement_m"] = round(displacement, 2)
            if retyped:
                entry["original_plant_type"] = original.plant_type
            if moved or retyped:
                entry["zone_check"] = "not_checked"
                entry["note"] = NOTE_MOVED if moved else NOTE_RETYPED
        explanations.append(entry)
    return explanations
