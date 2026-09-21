"""Declarative setback-distance table (743-ПП): obstacle category/subtype -> minimum
distance in meters, per plant type, with a citation.

Same load pattern as greenplan.rules.schema.RulePack -- a new provider's or a
new regulation's numbers are a YAML edit, not a code change.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class SetbackRule(BaseModel):
    obstacle_category: str
    # None means "matches any/no subtype" (e.g. road_edge has no subtype).
    obstacle_subtype: str | None = None
    tree_m: float
    shrub_m: float
    citation: str


class NormsTable(BaseModel):
    source: str
    rules: list[SetbackRule]

    @classmethod
    def load(cls, path: Path | str) -> "NormsTable":
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        return cls(source=str(path), rules=[SetbackRule.model_validate(item) for item in raw])

    def find(self, category: str, subtype: str | None) -> SetbackRule | None:
        for rule in self.rules:
            if rule.obstacle_category == category and rule.obstacle_subtype == subtype:
                return rule
        return None

    def distance_for(self, category: str, subtype: str | None, plant_type: str) -> float | None:
        rule = self.find(category, subtype)
        if rule is None:
            return None
        return rule.tree_m if plant_type == "tree" else rule.shrub_m
