"""ZoningResult -> one GeoJSON FeatureCollection, allowed and prohibited zones
both present, distinguished by a zone_type property (per the user's choice of
a single tagged file over two separate files).

Also includes three diagnostic "extent" features (zone_type site_boundary /
lawn_raw / base_area): the site boundary and lawn as extracted, and their
intersection. These let a human reviewer tell which stage a discrepancy
comes from -- e.g. a site_boundary that looks like the expected rectangle
but a lawn_raw that's empty/tiny points at a lawn-layer classification gap,
while a normal-looking lawn_raw whose base_area intersection is still tiny
points at a coordinate/scale mismatch between the two source layers.
"""

from __future__ import annotations

from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from greenplan.export.geojson import NO_CRS_LABEL
from greenplan.model import ZoningResult


def _polygonal_only(geom: BaseGeometry) -> BaseGeometry | None:
    """Difference/intersection ops on well-formed polygons should stay
    polygonal, but defensively drop any stray line/point slivers a
    GeometryCollection edge case could introduce, rather than emit invalid
    GeoJSON for downstream viewers.
    """
    if geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
        return unary_union(polys) if polys else None
    return None


def zoning_result_to_geojson(zoning: ZoningResult, crs: str = NO_CRS_LABEL) -> dict:
    features = []

    for zone_type, geom in (
        ("site_boundary", zoning.site_boundary),
        ("lawn_raw", zoning.lawn_raw),
        ("base_area", zoning.base_area),
    ):
        if geom is None:
            continue
        poly = _polygonal_only(geom)
        if poly is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(poly),
                "properties": {"zone_type": zone_type},
            }
        )

    for plant_type, geom in zoning.allowed.items():
        poly = _polygonal_only(geom)
        if poly is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(poly),
                "properties": {
                    "zone_type": "allowed",
                    "plant_type": plant_type,
                },
            }
        )

    for zone in zoning.prohibited:
        poly = _polygonal_only(zone.geometry)
        if poly is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(poly),
                "properties": {
                    "zone_type": "prohibited",
                    "plant_type": zone.plant_type,
                    "obstacle_category": zone.obstacle_category,
                    "obstacle_subtype": zone.obstacle_subtype,
                    "distance_m": zone.distance_m,
                    "citation": zone.citation,
                    "reason": zone.reason,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "metadata": {
            "used_site_boundary": zoning.used_site_boundary,
            "uncovered_categories": [
                {"category": c, "subtype": s} for c, s in zoning.uncovered_categories
            ],
            "crs": crs,
        },
        "features": features,
    }
