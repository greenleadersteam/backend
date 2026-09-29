"""Declarative setback-distance table (743-ПП): obstacle category/subtype -> minimum
distance in meters, per plant type, with a citation.

Same load pattern as greenplan.rules.schema.RulePack -- a new provider's or a
new regulation's numbers are a YAML edit, not a code change.

Each row also says where its numbers come from, per plant type: a value is
either taken from a normative act (`regulation`, with the act's clause) or is
the service's own conservative choice where the act sets none -- e.g. a "-"
in 743-ПП табл. 3.6.1's shrub column (`service_default`, no clause, and a
citation that doesn't name the act as if it required the value).

Pydantic/yaml only: the API process imports this for `GET /norms`, so it
must not pull in the geo stack.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

DEFAULT_NORMS_PATH = Path(__file__).parent / "default.yaml"

PlantType = Literal["tree", "shrub"]
NormBasis = Literal["regulation", "service_default"]


class PlantNorm(BaseModel):
    """Source of one plant type's value in a SetbackRule."""

    basis: NormBasis = "regulation"
    # Clause/table/row of the act; ignored (always null) for service_default.
    clause: str | None = None
    # Full text for the UI; defaults to the citation.
    text: str | None = None
    # Overrides the row's citation for this plant type -- required in
    # practice for service_default, whose value the act doesn't set.
    citation: str | None = None


class Norm(BaseModel):
    """One (obstacle, plant type) norm, resolved from a SetbackRule -- the
    `GET /norms` record, also what a prohibited zone cites."""

    id: str
    obstacle_category: str
    obstacle_subtype: str | None
    plant_type: PlantType
    distance_m: float
    basis: NormBasis
    citation: str
    act: str | None
    clause: str | None
    text: str
    source_url: str | None


class SetbackRule(BaseModel):
    # Stable row id, e.g. "743-pp-gas"; a Norm's id adds "-tree"/"-shrub".
    id: str = ""
    obstacle_category: str
    # None means "matches any/no subtype" (e.g. road_edge has no subtype).
    obstacle_subtype: str | None = None
    tree_m: float
    shrub_m: float
    citation: str
    act: str | None = None
    source_url: str | None = None
    tree: PlantNorm = Field(default_factory=PlantNorm)
    shrub: PlantNorm = Field(default_factory=PlantNorm)

    @model_validator(mode="after")
    def _default_id(self) -> "SetbackRule":
        if not self.id:
            self.id = f"{self.obstacle_category}-{self.obstacle_subtype or 'any'}"
        return self

    def distance(self, plant_type: str) -> float:
        return self.tree_m if plant_type == "tree" else self.shrub_m

    def norm(self, plant_type: PlantType) -> Norm:
        source = self.tree if plant_type == "tree" else self.shrub
        citation = source.citation or self.citation
        return Norm(
            id=f"{self.id}-{plant_type}",
            obstacle_category=self.obstacle_category,
            obstacle_subtype=self.obstacle_subtype,
            plant_type=plant_type,
            distance_m=self.distance(plant_type),
            basis=source.basis,
            citation=citation,
            act=self.act,
            clause=source.clause if source.basis == "regulation" else None,
            text=source.text or citation,
            source_url=self.source_url,
        )


class NormsTable(BaseModel):
    source: str
    rules: list[SetbackRule]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_NORMS_PATH) -> "NormsTable":
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
        return rule.distance(plant_type)

    def norms(self) -> list[Norm]:
        return [rule.norm(plant_type) for rule in self.rules for plant_type in ("tree", "shrub")]
