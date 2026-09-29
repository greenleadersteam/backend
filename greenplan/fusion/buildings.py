"""Merge the plan's buildings with Overture building footprints.

The plan is the more accurate source (a 1:500 survey) but mostly draws
buildings as loose wall lines, not polygons; Overture has complete closed
footprints but is off by ~1-3m, plus the georeferencing residual. So:

1. Plan polygon -> kept as-is; the best-overlapping Overture footprint only
   contributes attributes (class, floors, height, name).
2. Overture footprint not covered by a plan polygon -> added. Tagged
   `overture_confirmed` when plan wall lines run along its outline,
   `overture_unconfirmed` when the plan has no evidence of it at all (could be
   demolished, or just outside the surveyed area -- kept, since excluding
   space is the safe side for planting).
3. Plan wall lines and point symbols -> kept unchanged. They're the surveyed
   wall positions, so their own setback buffer stays even where an Overture
   footprint covers the same building.

Every building polygon (plan or Overture) is flagged `school_kindergarten`
(a larger setback, see norms/default.yaml) if its Overture class says so, or
if it mostly lies inside an Overture education land-use area of that class --
building-level class is sparse (~13% of Moscow buildings have one), while
school/kindergarten grounds are mapped far more consistently.

All geometry is in the same metric CRS (UTM after georeferencing).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from shapely import STRtree
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from pydantic import BaseModel

from greenplan.geometry import repair_polygon
from greenplan.model import Feature, FeatureCollection

BUILDINGS_CATEGORY = "buildings"
BUILDINGS_RULE_ID = "4"
SCHOOL_SUBTYPE = "school_kindergarten"
SCHOOL_CLASSES = frozenset({"school", "kindergarten"})

# A plan polygon and an Overture footprint describe the same building when
# their overlap covers this share of the smaller of the two.
MATCH_OVERLAP_SHARE = 0.5
# An Overture footprint is redundant (the plan already has it as a polygon)
# when plan polygons cover this share of it.
REDUNDANT_COVER_SHARE = 0.5
# Plan wall lines within this distance of a footprint outline count as
# evidence for it: ~Overture offset + georeferencing residual.
EDGE_TOLERANCE_M = 2.0
# Share of a footprint's outline length that must be backed by plan lines
# for it to be `overture_confirmed`.
EDGE_SUPPORT_MIN = 0.3
# Share of a building's area inside a school/kindergarten land-use area for
# it to count as a school/kindergarten building.
SCHOOL_AREA_SHARE = 0.5
# Land-use inference applies only to unclassified buildings at least this
# big: school grounds also hold verandas/sheds/boiler houses (12-113 m² on
# Старый Гай), while real school/kindergarten buildings there were 900-1850 m².
SCHOOL_MIN_BUILDING_AREA_M2 = 300.0


@dataclass
class OvertureBuilding:
    id: str
    geometry: BaseGeometry
    cls: str | None = None
    subtype: str | None = None
    height: float | None = None
    num_floors: int | None = None
    name: str | None = None


@dataclass
class EducationArea:
    geometry: BaseGeometry
    cls: str


class BuildingFusionReport(BaseModel):
    plan_polygons: int = 0
    plan_polygons_with_overture_attrs: int = 0
    plan_lines_and_points: int = 0
    overture_footprints: int = 0
    overture_redundant: int = 0
    overture_confirmed: int = 0
    overture_unconfirmed: int = 0
    school_kindergarten: int = 0


@dataclass
class _SchoolIndex:
    areas: list[EducationArea]
    tree: STRtree | None = field(init=False)

    def __post_init__(self):
        self.tree = STRtree([a.geometry for a in self.areas]) if self.areas else None

    def area_class(self, geom: BaseGeometry) -> str | None:
        if self.tree is None or geom.area <= 0:
            return None
        for i in self.tree.query(geom, predicate="intersects"):
            area = self.areas[i]
            if geom.intersection(area.geometry).area / geom.area >= SCHOOL_AREA_SHARE:
                return area.cls
        return None


def _is_polygonal(geom: BaseGeometry) -> bool:
    return geom.geom_type in ("Polygon", "MultiPolygon")


def _is_linear(geom: BaseGeometry) -> bool:
    return geom.geom_type in ("LineString", "MultiLineString", "LinearRing")


def _school_evidence(cls: str | None, geom: BaseGeometry, schools: _SchoolIndex) -> str | None:
    if cls in SCHOOL_CLASSES:
        return f"overture_building_class:{cls}"
    if cls is not None or geom.area < SCHOOL_MIN_BUILDING_AREA_M2:
        return None
    area_cls = schools.area_class(geom)
    return f"overture_land_use:{area_cls}" if area_cls else None


def _overture_attrs(o: OvertureBuilding) -> dict:
    return {
        "overture_id": o.id,
        "overture_class": o.cls,
        "overture_subtype": o.subtype,
        "height_m": o.height,
        "num_floors": o.num_floors,
        "name": o.name,
    }


def fuse_buildings(
    fc: FeatureCollection,
    overture_buildings: list[OvertureBuilding],
    education_areas: list[EducationArea],
    *,
    source_label: str = "overture",
) -> tuple[FeatureCollection, BuildingFusionReport]:
    report = BuildingFusionReport()
    schools = _SchoolIndex([a for a in education_areas if a.cls in SCHOOL_CLASSES])

    overture = []
    for o in overture_buildings:
        geom = repair_polygon(o.geometry) if _is_polygonal(o.geometry) else None
        if geom is not None and geom.area > 0:
            overture.append(replace(o, geometry=geom))
    report.overture_footprints = len(overture)
    overture_tree = STRtree([o.geometry for o in overture]) if overture else None

    plan_poly_idx = [
        i for i, f in enumerate(fc.features) if f.category == BUILDINGS_CATEGORY and _is_polygonal(f.geometry)
    ]
    plan_line_geoms = [
        f.geometry for f in fc.features if f.category == BUILDINGS_CATEGORY and _is_linear(f.geometry)
    ]
    report.plan_polygons = len(plan_poly_idx)
    report.plan_lines_and_points = sum(
        1 for f in fc.features if f.category == BUILDINGS_CATEGORY and not _is_polygonal(f.geometry)
    )

    features = list(fc.features)

    # 1. Plan polygons: keep geometry, borrow attributes from the best match.
    for i in plan_poly_idx:
        f = features[i]
        best, best_overlap = None, 0.0
        if overture_tree is not None:
            for j in overture_tree.query(f.geometry, predicate="intersects"):
                o = overture[j]
                overlap = f.geometry.intersection(o.geometry).area
                smaller = min(f.geometry.area, o.geometry.area)
                if smaller > 0 and overlap / smaller >= MATCH_OVERLAP_SHARE and overlap > best_overlap:
                    best, best_overlap = o, overlap
        extra = {**f.extra, "source": "plan"}
        if best is not None:
            extra.update(_overture_attrs(best))
            report.plan_polygons_with_overture_attrs += 1
        evidence = _school_evidence(best.cls if best else None, f.geometry, schools)
        subtype = f.subtype
        if evidence:
            subtype = SCHOOL_SUBTYPE
            extra["school_kindergarten_evidence"] = evidence
            report.school_kindergarten += 1
        features[i] = f.model_copy(update={"subtype": subtype, "extra": extra})

    # 2. Overture footprints the plan doesn't already have as a polygon.
    plan_poly_union = unary_union([fc.features[i].geometry for i in plan_poly_idx]) if plan_poly_idx else None
    line_tree = STRtree(plan_line_geoms) if plan_line_geoms else None
    for o in overture:
        if plan_poly_union is not None:
            covered = o.geometry.intersection(plan_poly_union).area / o.geometry.area
            if covered >= REDUNDANT_COVER_SHARE:
                report.overture_redundant += 1
                continue

        support = 0.0
        outline = o.geometry.boundary
        if line_tree is not None and outline.length > 0:
            corridor = outline.buffer(EDGE_TOLERANCE_M)
            near = [plan_line_geoms[k] for k in line_tree.query(corridor, predicate="intersects")]
            if near:
                # Supported outline length: the part of the outline that has a
                # plan line within tolerance (not the plan lines' own length,
                # which double-counts parallel/duplicate strokes).
                backing = unary_union(near).buffer(EDGE_TOLERANCE_M)
                support = outline.intersection(backing).length / outline.length
        status = "overture_confirmed" if support >= EDGE_SUPPORT_MIN else "overture_unconfirmed"
        if status == "overture_confirmed":
            report.overture_confirmed += 1
        else:
            report.overture_unconfirmed += 1

        extra = {"source": "overture", **_overture_attrs(o), "edge_support": round(support, 3)}
        subtype = None
        evidence = _school_evidence(o.cls, o.geometry, schools)
        if evidence:
            subtype = SCHOOL_SUBTYPE
            extra["school_kindergarten_evidence"] = evidence
            report.school_kindergarten += 1
        features.append(
            Feature(
                geometry=o.geometry,
                category=BUILDINGS_CATEGORY,
                subtype=subtype,
                rule_id=BUILDINGS_RULE_ID,
                status=status,
                layer="Overture: building",
                dxftype="OVERTURE",
                source_file=source_label,
                extra=extra,
            )
        )

    return fc.model_copy(update={"features": features}), report
