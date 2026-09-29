from __future__ import annotations

from datetime import datetime

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from greenplan.api.jobs import GeoreferenceInfo, JobError, JobRecord
from greenplan.api.plantings import ExportError, PlantingVersion


# Surrounding whitespace is trimmed; a blank name is rejected (422).
ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
ProjectDescription = Annotated[str, StringConstraints(max_length=1000)]


class ProjectCreateRequest(BaseModel):
    name: ProjectName
    description: ProjectDescription | None = None
    # (minx, miny, maxx, maxy), WGS84 lon/lat: an approximate bbox of the
    # project site, required to disambiguate geobridge.ru geodetic-point
    # matches during the mandatory georeferencing stage -- see
    # greenplan.georeference.transform's module docstring for why this can't
    # be skipped (catalog point numbers are not globally unique).
    bbox_user: tuple[float, float, float, float]


class ProjectUpdateRequest(BaseModel):
    name: ProjectName | None = None
    description: ProjectDescription | None = None


class JobStatus(BaseModel):
    stage: str
    progress_pct: int
    error: JobError | None = None
    georeference: GeoreferenceInfo | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_record(cls, record: JobRecord | None) -> "JobStatus":
        if record is None:
            return cls(stage="draft", progress_pct=0)
        return cls(**record.model_dump())


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    status: str
    job: JobStatus


class PlantingEditResponse(BaseModel):
    version: PlantingVersion = Field(description="Metadata of the newly created version.")
    id_map: dict[str, str] = Field(
        description="`client_id` of each `add` item -> the point id the server assigned to it."
    )


class ExportPendingResponse(BaseModel):
    """Returned with `202` while a version's DXF/explanation is being
    generated. Poll `GET /projects/{id}/plantings` until this version's
    `export_status` is `ready` (or `failed`), then request the file again.
    """

    version: int
    export_status: Literal["pending"]


class ErrorMessage(BaseModel):
    detail: str


class ErrorWithCode(BaseModel):
    detail: ExportError


# -- Planting GeoJSON (read-only response documentation; the routes return the
# stored file as-is) --


class PointGeometry(BaseModel):
    type: Literal["Point"]
    coordinates: tuple[float, float] = Field(
        description="[lon, lat] in WGS84 (EPSG:4326). Projects without georeferencing "
        "(only very old ones) use the drawing's own local x/y instead."
    )


class PlantingProperties(BaseModel):
    id: str = Field(
        description="Opaque string id. Automatic points (e.g. `TREE_ROW_CURB-00012`) keep the "
        "same id in every version; manual points get `manual-NNNNN`, never reused within a project."
    )
    plant_type: Literal["tree", "shrub"]
    kind: Literal["auto", "manual"] = Field(
        description="`auto`: placed by the layout engine (possibly moved/retyped since). "
        "`manual`: added by a user edit."
    )
    rule_id: str | None = Field(description="Layout rule that placed the point; `null` for manual points.")
    added_in_version: int | None = Field(
        default=None, description="Manual points only: the version that added the point."
    )


class PlantingFeature(BaseModel):
    type: Literal["Feature"]
    geometry: PointGeometry
    properties: PlantingProperties


class GeoJSONMetadata(BaseModel):
    crs: str = Field(description='Normally "EPSG:4326 (WGS84 lon/lat)".')


class PlantingFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"]
    metadata: GeoJSONMetadata
    features: list[PlantingFeature]


class ExplanationEntry(BaseModel):
    """One point's explanation. Coordinates are in the DXF drawing's local
    frame (they match the DXF, not the GeoJSON). Fields marked "edited
    versions" are present only where they apply.
    """

    id: str
    plant_type: Literal["tree", "shrub"]
    kind: Literal["auto", "manual"]
    rule_id: str | None
    rule_name_ru: str | None = Field(description="Human-readable name of the layout rule, if any.")
    x: float = Field(description="Drawing-frame x, meters.")
    y: float = Field(description="Drawing-frame y, meters.")
    moved: bool | None = Field(
        default=None, description="Auto points: moved from their version-1 position (by at least 1 cm)."
    )
    displacement_m: float | None = Field(
        default=None, description="Auto points, if moved: distance from the version-1 position, meters."
    )
    original_plant_type: Literal["tree", "shrub"] | None = Field(
        default=None, description="Auto points, if their plant_type was changed: the version-1 type."
    )
    added_in_version: int | None = Field(default=None, description="Manual points: the version that added them.")
    zone_check: Literal["not_checked"] | None = Field(
        default=None,
        description="Present for manual and moved/retyped points: the backend does not check "
        "zones for edits, so their placement is not verified against setback norms.",
    )
    note: str | None = Field(default=None, description="Human-readable (Russian) note for edited points.")


# -- Obstacles GeoJSON (read-only response documentation, as above) --


class ObstacleGeometry(BaseModel):
    type: Literal["Point", "LineString", "MultiLineString", "Polygon", "MultiPolygon"]
    coordinates: list = Field(description="Same CRS as `/zones`: WGS84 lon/lat, or drawing x/y.")


class ObstacleProperties(BaseModel):
    """As in the parse output, minus the server-side `source_file`. Extra
    keys may be present (`label` for geodetic points, `elevation` for
    contours, Overture attributes for fused buildings/roads)."""

    model_config = ConfigDict(extra="allow")

    rule_id: str = Field(description="Parser rule (`rules/default.yaml`) that recognised the entity.")
    category: str = Field(description="Same as `obstacle_category` in `/zones`.")
    subtype: str | None = Field(description="Same as `obstacle_subtype` in `/zones`.")
    status: str = Field(description="Rule status, e.g. `auto`, `proxy_low_confidence`.")
    layer: str = Field(description="Source DXF layer.")
    dxftype: str = Field(description="DXF entity type, e.g. `LWPOLYLINE`; `OVERTURE` for fused Overture data.")
    handle: str | None = Field(description="DXF handle; `null` for xref/exploded-block entities.")


class ObstacleFeature(BaseModel):
    type: Literal["Feature"]
    geometry: ObstacleGeometry
    properties: ObstacleProperties


class ObstaclesMetadata(BaseModel):
    crs: str = Field(
        description='"EPSG:4326 (WGS84 lon/lat)" once georeferenced; otherwise (including a '
        'project that failed at georeferencing) "local drawing coordinates, no geo-reference available".'
    )
    source_insunits: int | None = Field(default=None, description="`$INSUNITS` of the root DXF.")
    scale_to_meters: float | None = Field(default=None, description="Drawing units -> meters factor.")


class ObstaclesFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"]
    metadata: ObstaclesMetadata
    features: list[ObstacleFeature]
