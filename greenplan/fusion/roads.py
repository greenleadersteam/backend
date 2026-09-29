"""Carriageway polygons from the plan + Overture road centerlines, and kerb
classification by what each kerb borders.

Carriageway polygons, in priority order:
  A. The plan's own carriageway fills (`carriageway` features from the
     parser) -- kept as drawn.
  B. Built from kerb linework: the site is split into faces by every barrier
     line (kerbs, footpath edges, buildings, lawn edges, site boundary, plan
     carriageway edges); a face crossed by an Overture vehicle-road centerline
     is carriageway. Kerbs are often dashed linetypes exploded into 0.5-0.7m
     pieces, so barriers are thickened by BARRIER_BRIDGE_M to close the dash
     gaps. A face whose area mostly lies within MAX_HALF_WIDTH of its
     centerlines is kerb-bounded and taken as-is; a face that leaks beyond it
     (kerb gap at a driveway/crossing, or no kerbs at all) is clipped to the
     centerline buffered by the class's default half-width instead.
  Known non-carriageway surfaces (plan lawns, footpaths, building polygons,
  tier A) always win over tier B.

Street category (the setback subtype, see norms/default.yaml): the plan's
pavement-work hints ("..._ПЧ_магистральные"/"..._Местные") decide arterial vs
local; within arterials, Overture decides citywide vs district; without a
hint, the Overture class decides. Several centerlines -> the higher category.

Kerbs (`road_edge`) within the fusion extent are then split: parts along a
carriageway edge become `road_edge/<street category>`; parts near (but not
along) a carriageway become `footpath_edge` -- a sidewalk/lawn kerb; parts
far from any known carriageway stay `road_edge` with the generic norm.

All geometry is in UTM meters.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel
import shapely
from shapely import STRtree
from shapely.geometry import MultiLineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from greenplan.model import Feature, FeatureCollection

CARRIAGEWAY = "carriageway"
KERB = "road_edge"
FOOTPATH = "footpath_edge"

VEHICLE_CLASS_CATEGORY = {
    "motorway": "arterial_citywide",
    "trunk": "arterial_citywide",
    "primary": "arterial_citywide",
    "secondary": "arterial_district",
    "tertiary": "local",
    "residential": "local",
    "unclassified": "local",
    "living_street": "local",
    "service": "driveway",
}
CATEGORY_RANK = {"driveway": 1, "local": 2, "arterial_district": 3, "arterial": 4, "arterial_citywide": 5}

# Typical Moscow half-widths of the carriageway per Overture class (m). Only
# used where kerbs don't bound the road; MAX_HALF_WIDTH is the leak test.
DEFAULT_HALF_WIDTH_M = {
    "motorway": 15.0, "trunk": 12.0, "primary": 10.5, "secondary": 7.5, "tertiary": 5.25,
    "residential": 3.5, "unclassified": 3.5, "living_street": 3.0, "service": 2.75,
}
CENTERLINE_OFFSET_M = 3.0  # Overture centerline position error + georeferencing residual


def max_half_width(cls: str) -> float:
    return 2 * DEFAULT_HALF_WIDTH_M[cls] + CENTERLINE_OFFSET_M


BARRIER_BRIDGE_M = 0.5  # closes gaps < 1m between barrier pieces (dashed kerbs)
BOUNDED_FACE_SLACK = 1.25  # face area / area within max half-width
MIN_CROSSING_M = 2.0  # centerline length inside a face/polygon to count
MIN_CATEGORY_SEGMENT_M = 5.0  # centerline length inside a polygon to set its category
KNOWN_SURFACE_SHARE = 0.5  # face mostly lawn/footpath/building -> not carriageway
MIN_CARRIAGEWAY_AREA_M2 = 5.0
KERB_EDGE_TOLERANCE_M = 1.5  # kerb within this of a carriageway edge borders it
SIDEWALK_KERB_REACH_M = 25.0  # kerbs this near a carriageway, not on its edge -> sidewalk kerbs
MIN_KERB_PIECE_M = 0.05


@dataclass
class RoadSegment:
    id: str
    geometry: BaseGeometry  # LineString in UTM
    cls: str


class RoadFusionReport(BaseModel):
    vehicle_segments: int = 0
    plan_carriageways: int = 0
    plan_street_hints: int = 0
    fused_kerb_bounded: int = 0
    fused_default_width: int = 0
    kerb_m_carriageway_edge: float = 0.0
    kerb_m_sidewalk: float = 0.0
    kerb_m_unchanged: float = 0.0


def _polygonal(geom: BaseGeometry) -> bool:
    return geom.geom_type in ("Polygon", "MultiPolygon")


def _parts(geom: BaseGeometry) -> list[BaseGeometry]:
    if geom.is_empty:
        return []
    return list(geom.geoms) if hasattr(geom, "geoms") else [geom]


def _outline(geom: BaseGeometry) -> BaseGeometry:
    return geom.boundary if _polygonal(geom) else geom


def _linear_part(geom: BaseGeometry) -> BaseGeometry | None:
    lines = [g for g in _parts(geom) if g.geom_type in ("LineString", "LinearRing") and g.length >= MIN_KERB_PIECE_M]
    if not lines:
        return None
    return lines[0] if len(lines) == 1 else MultiLineString(lines)


def resolve_category(hint: str | None, overture_category: str | None) -> str | None:
    if hint == "local":
        return "local"
    if hint == "arterial":
        if overture_category == "arterial_citywide":
            return "arterial_citywide"
        return "arterial_district" if overture_category else "arterial"
    return overture_category


def _highest(categories) -> str | None:
    ranked = [c for c in categories if c]
    return max(ranked, key=CATEGORY_RANK.__getitem__) if ranked else None


def _overture_category(geom: BaseGeometry, segments: list[RoadSegment], seg_tree: STRtree | None) -> str | None:
    if seg_tree is None:
        return None
    cats = []
    for i in seg_tree.query(geom, predicate="intersects"):
        if segments[i].geometry.intersection(geom).length >= MIN_CATEGORY_SEGMENT_M:
            cats.append(VEHICLE_CLASS_CATEGORY[segments[i].cls])
    return _highest(cats)


def _hint_for(geom: BaseGeometry, hints: list[Feature]) -> str | None:
    found = {h.subtype for h in hints if h.geometry.intersects(geom) and h.geometry.intersection(geom).area > 1.0}
    if "arterial" in found:
        return "arterial"
    return "local" if "local" in found else None


def fuse_roads(
    fc: FeatureCollection,
    segments: list[RoadSegment],
    extent: Polygon,
    *,
    source_label: str = "overture",
) -> tuple[FeatureCollection, RoadFusionReport]:
    report = RoadFusionReport()
    segments = [
        RoadSegment(s.id, clipped, s.cls)
        for s in segments
        if s.cls in VEHICLE_CLASS_CATEGORY
        for clipped in [s.geometry.intersection(extent)]
        if not clipped.is_empty and clipped.length > 0
    ]
    report.vehicle_segments = len(segments)
    seg_tree = STRtree([s.geometry for s in segments]) if segments else None

    features = list(fc.features)
    plan_cw_idx = [i for i, f in enumerate(features) if f.category == CARRIAGEWAY and _polygonal(f.geometry)]
    hints = [features[i] for i in plan_cw_idx if features[i].subtype in ("arterial", "local")]
    report.plan_carriageways = len(plan_cw_idx)
    report.plan_street_hints = len(hints)

    # A. Plan carriageway polygons: keep geometry, refine the street category.
    for i in plan_cw_idx:
        f = features[i]
        hint = f.subtype if f.subtype in ("arterial", "local") else _hint_for(f.geometry, hints)
        ov = _overture_category(f.geometry, segments, seg_tree)
        category = resolve_category(hint, ov)
        features[i] = f.model_copy(update={
            "subtype": category,
            "extra": {**f.extra, "source": "plan", "street_category_hint": hint, "overture_street_category": ov},
        })

    # B. Faces from barrier linework, labelled by vehicle centerlines.
    if segments:
        features.extend(_fused_carriageways(features, plan_cw_idx, hints, segments, seg_tree, extent, report, source_label))

    carriageways = [f for f in features if f.category == CARRIAGEWAY and _polygonal(f.geometry)]
    if carriageways:
        features = _classify_kerbs(features, carriageways, extent, report)
    return fc.model_copy(update={"features": features}), report


def _thicken(geom: BaseGeometry) -> BaseGeometry:
    # Same 1cm snap as zoning (sub-mm drawing noise makes GEOS buffers hang or
    # come out invalid); make_valid for the rare invalid buffer that remains,
    # which otherwise breaks the union with a TopologyException.
    buffered = shapely.set_precision(geom, grid_size=0.01).buffer(BARRIER_BRIDGE_M)
    return buffered if buffered.is_valid else shapely.make_valid(buffered)


def _fused_carriageways(features, plan_cw_idx, hints, segments, seg_tree, extent, report, source_label):
    barrier_geoms = []
    known_surfaces = []
    for i, f in enumerate(features):
        g = f.geometry
        if not g.intersects(extent) or g.geom_type == "Point":
            continue
        is_lawn = f.category == "green_existing" and f.subtype == "lawn"
        if f.category in (KERB, FOOTPATH, "buildings", "site_boundary", CARRIAGEWAY) or is_lawn:
            barrier_geoms.append(_outline(g))
        if _polygonal(g) and (is_lawn or f.category in (FOOTPATH, "buildings") or i in plan_cw_idx):
            known_surfaces.append(g)
    known = unary_union(known_surfaces) if known_surfaces else None

    barrier = unary_union([_thicken(g) for g in barrier_geoms if not g.is_empty])
    free = extent.difference(barrier)
    faces = [p.buffer(BARRIER_BRIDGE_M).intersection(extent) for p in _parts(free) if p.area > 1.0]
    out = []
    for face in faces:
        crossing = [
            segments[i] for i in seg_tree.query(face, predicate="intersects")
            if segments[i].geometry.intersection(face).length >= MIN_CROSSING_M
        ]
        if not crossing:
            continue
        if known is not None and face.intersection(known).area >= KNOWN_SURFACE_SHARE * face.area:
            continue
        ribbon_max = unary_union([s.geometry.buffer(max_half_width(s.cls)) for s in crossing])
        bounded = face.area <= BOUNDED_FACE_SLACK * face.intersection(ribbon_max).area
        if bounded:
            geom = face
        else:
            geom = face.intersection(unary_union([s.geometry.buffer(DEFAULT_HALF_WIDTH_M[s.cls]) for s in crossing]))
        if known is not None:
            geom = geom.difference(known)
        geom = unary_union([p for p in _parts(geom) if _polygonal(p) and p.area >= MIN_CARRIAGEWAY_AREA_M2])
        if geom.is_empty:
            continue
        if bounded:
            report.fused_kerb_bounded += 1
        else:
            report.fused_default_width += 1
        hint = _hint_for(geom, hints)
        ov = _highest(VEHICLE_CLASS_CATEGORY[s.cls] for s in crossing)
        out.append(Feature(
            geometry=geom,
            category=CARRIAGEWAY,
            subtype=resolve_category(hint, ov),
            rule_id="5b",
            status="fused_kerb_bounded" if bounded else "fused_default_width",
            layer="Overture: segment + plan kerbs",
            dxftype="FUSED",
            source_file=source_label,
            extra={
                "source": "fused",
                "overture_ids": sorted({s.id for s in crossing}),
                "overture_classes": sorted({s.cls for s in crossing}),
                "street_category_hint": hint,
                "overture_street_category": ov,
            },
        ))
    return out


def _classify_kerbs(features, carriageways, extent, report):
    by_category: dict[str | None, list[BaseGeometry]] = {}
    for c in carriageways:
        by_category.setdefault(c.subtype, []).append(c.geometry.boundary.buffer(KERB_EDGE_TOLERANCE_M))
    ranked = sorted(by_category, key=lambda c: CATEGORY_RANK.get(c, 0), reverse=True)
    corridors = [(cat, unary_union(by_category[cat])) for cat in ranked]
    near_zone = unary_union([c.geometry for c in carriageways]).buffer(SIDEWALK_KERB_REACH_M).intersection(extent)

    out = []
    for f in features:
        if f.category != KERB or f.geometry.geom_type == "Point":
            out.append(f)
            continue
        remaining = f.geometry
        for cat, corridor in corridors:
            piece = _linear_part(remaining.intersection(corridor))
            if piece is None:
                continue
            report.kerb_m_carriageway_edge += piece.length
            out.append(f.model_copy(update={"geometry": piece, "subtype": cat,
                                            "extra": {**f.extra, "kerb_side": "carriageway"}}))
            remaining = remaining.difference(corridor)
        sidewalk = _linear_part(remaining.intersection(near_zone))
        if sidewalk is not None:
            report.kerb_m_sidewalk += sidewalk.length
            out.append(f.model_copy(update={"geometry": sidewalk, "category": FOOTPATH, "subtype": None,
                                            "extra": {**f.extra, "kerb_side": "sidewalk", "reclassified_from": KERB}}))
            remaining = remaining.difference(near_zone)
        rest = _linear_part(remaining)
        if rest is not None:
            report.kerb_m_unchanged += rest.length
            out.append(f.model_copy(update={"geometry": rest}))
    return out
