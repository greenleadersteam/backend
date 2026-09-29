"""Canonical feature schema, decoupled from DXF.

`category` and `subtype` are free-form strings rather than a hardcoded enum
on purpose: they come straight from whatever `rules/*.yaml` rule pack was
used to classify the entity, so a new provider's layer convention can add a
new category without a code change (see `greenplan.rules`).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from shapely.geometry.base import BaseGeometry


class Feature(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    geometry: BaseGeometry
    category: str
    subtype: str | None = None
    rule_id: str
    status: str
    layer: str
    dxftype: str
    source_file: str
    # None for entities that came from a resolved xref (entity.copy() makes
    # an unbound copy with no real DXF handle) or from exploding a block
    # reference (virtual_entities() children aren't bound to any document).
    handle: str | None = None
    extra: dict[str, Any] = {}


class FeatureCollection(BaseModel):
    root_file: str
    source_folder: str
    insunits_code: int
    scale_to_meters: float
    features: list[Feature] = []


class ProhibitedZone(BaseModel):
    """One grouped prohibited-zone polygon: the union of every obstacle of a
    given (category, subtype) buffered by its setback distance for one plant
    type, clipped to the base plantable area.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    geometry: BaseGeometry
    plant_type: str  # "tree" | "shrub"
    obstacle_category: str
    obstacle_subtype: str | None = None
    distance_m: float
    citation: str
    reason: str
    # Source of the norm (greenplan.norms.schema.Norm): its id, whether the
    # value is from an act or the service's own, and the act's clause/URL.
    norm_id: str | None = None
    basis: str | None = None
    clause: str | None = None
    source_url: str | None = None


class ZoningResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    plant_types: list[str]
    # Raw intermediate extents, kept around (not just the final base_area) so
    # a human reviewer can tell which stage a discrepancy comes from: the
    # site boundary as extracted, the lawn as extracted, and their
    # intersection (base_area) -- see export.zones.zoning_result_to_geojson.
    lawn_raw: BaseGeometry
    site_boundary: BaseGeometry | None
    base_area: BaseGeometry
    # base_area was clipped to a detected site boundary; if False, the full
    # lawn extent was used because no site_boundary feature was found.
    used_site_boundary: bool
    allowed: dict[str, BaseGeometry]
    prohibited: list[ProhibitedZone]
    uncovered_categories: list[tuple[str, str | None]]  # (category, subtype) with no norms entry


class PlantingPoint(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    geometry: BaseGeometry  # Point
    plant_type: str  # "tree" | "shrub"
    # None for manually placed points (see greenplan.api.plantings) -- no
    # layout rule put them there.
    rule_id: str | None
    kind: str = "auto"  # "auto" (layout engine) | "manual" (user edit)
