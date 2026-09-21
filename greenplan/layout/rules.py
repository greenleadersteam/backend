"""Declarative planting-pattern table: how new trees/shrubs get laid out
inside the allowed zone. Ported from the prototype's full4_run.py (row/hedge
along curbs) and full5_fill.py/full6_shrubfill.py (grid fill), kept at their
documented MVP-coarsened spacing values (e.g. a 0.4m realistic hedge would be
much denser -- see the YAML comment).

Rule order matters: within the same plant_type, earlier rules' output is
added to the mutual-spacing exclusion set for later rules (row-then-fill, as
in the prototype), so the default YAML lists row/curb rules before fill
rules for each plant type.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class PlantingRule(BaseModel):
    id: str
    plant_type: str  # "tree" | "shrub"
    placement: str  # "row_along_curb" | "grid_fill"
    name_ru: str
    spacing_m: float
    offset_m: float | None = None  # required for row_along_curb, unused for grid_fill


class PlantingRuleSet(BaseModel):
    source: str
    rules: list[PlantingRule]

    @classmethod
    def load(cls, path: Path | str) -> "PlantingRuleSet":
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        return cls(source=str(path), rules=[PlantingRule.model_validate(item) for item in raw])

    def find(self, rule_id: str) -> PlantingRule | None:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        return None
