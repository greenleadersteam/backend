"""Turn a parsed FeatureCollection into allowed/prohibited planting-zone
polygons per plant type, by buffering each obstacle category/subtype by its
743-ПП setback distance and subtracting from the lawn.

v1 scope (per the plan): the candidate area is lawn ∩ site boundary (or the
full lawn if no site boundary was found -- logged, not silently assumed
safe). The prototype's "verified utility-survey extent" concept is deferred.
"""

from __future__ import annotations

import sys
from collections import defaultdict

import shapely
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from greenplan.model import FeatureCollection, ProhibitedZone, ZoningResult
from greenplan.norms.schema import NormsTable
from greenplan.zoning.topology import close_line_soup, resolve_nesting

PLANT_TYPES = ("tree", "shrub")

# Drawings occasionally contain clusters of many sub-millimeter-scale segments
# (a symbol-library texture/hatch pattern exploded from a block, e.g. a
# "откос"/slope-hatch detail) that are geometrically meaningless at
# planting-setback scale but pathological for GEOS to buffer: a MultiLineString
# of ~100+ segments each under 1mm long, all within a few cm of each other,
# was observed to make a single .buffer(2.0) call hang indefinitely (still not
# done after 3+ minutes). Snapping every obstacle geometry to a 1cm grid
# before any union/buffer collapses this near-duplicate noise (237 points ->
# 8 in the case that surfaced this) without affecting real setback geometry,
# whose relevant scale is meters, not millimeters.
_SNAP_GRID_SIZE_M = 0.01


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _group_geometries(fc: FeatureCollection) -> dict[tuple[str, str | None], list[BaseGeometry]]:
    groups: dict[tuple[str, str | None], list[BaseGeometry]] = defaultdict(list)
    for f in fc.features:
        snapped = shapely.set_precision(f.geometry, grid_size=_SNAP_GRID_SIZE_M)
        if snapped.is_empty:
            continue
        groups[(f.category, f.subtype)].append(snapped)
    return groups


def _polygonal_union(geoms: list[BaseGeometry]) -> BaseGeometry | None:
    """Combine a mix of polygons and lines into one polygon(-with-holes)
    geometry: close loose line soup (e.g. a site boundary drawn as separate
    LINE/LWPOLYLINE segments, possibly with small gaps at the seams between
    them -- see zoning.topology.close_line_soup) into polygons, then
    reconstruct even-odd nesting (a polygon fully inside another becomes a
    hole of it, not just absorbed by union -- see
    zoning.topology.resolve_nesting) across everything, closed-line-derived
    or already-a-polygon alike.
    """
    polys = [g for g in geoms if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
    lines = [g for g in geoms if g.geom_type in ("LineString", "MultiLineString") and not g.is_empty]
    if lines:
        closed = close_line_soup(lines)
        if not closed:
            log(f"WARN: {len(lines)} line fragment(s) in a boundary/lawn category could not be "
                f"closed into any polygon (even after endpoint snapping) -- they contribute no "
                f"area, which may silently shrink the resulting extent")
        polys.extend(closed)
    polys = [p for p in polys if not p.is_empty]
    if not polys:
        return None
    return resolve_nesting(polys)


# Categories that define the candidate area itself, not an obstacle to buffer
# away from -- excluded from the "norms coverage" gap report.
_STRUCTURAL_CATEGORIES = {("green_existing", "lawn"), ("site_boundary", None)}


def _part_count(geom: BaseGeometry) -> int:
    return len(geom.geoms) if hasattr(geom, "geoms") else 1


def compute_zones(
    fc: FeatureCollection,
    norms: NormsTable,
    plant_types: tuple[str, ...] = PLANT_TYPES,
    verbose: bool = False,
) -> ZoningResult:
    groups = _group_geometries(fc)

    lawn = _polygonal_union(groups.get(("green_existing", "lawn"), []))
    if lawn is None:
        log("WARN: no lawn ('газон') geometry found -- zoning will produce an empty allowed area")
        lawn = unary_union([])
    elif verbose:
        log(f"lawn_raw: {lawn.area:.1f} m^2, {_part_count(lawn)} part(s)")

    site_boundary = _polygonal_union(groups.get(("site_boundary", None), []))
    used_site_boundary = site_boundary is not None
    if used_site_boundary:
        base_area = lawn.intersection(site_boundary)
        if verbose:
            log(f"site_boundary: {site_boundary.area:.1f} m^2, {_part_count(site_boundary)} part(s)")
    else:
        log("WARN: no site boundary ('Граница работ') found -- "
            "using the full lawn extent as the candidate area")
        base_area = lawn
    if verbose:
        log(f"base_area (lawn_raw {'∩ site_boundary' if used_site_boundary else '(no site_boundary)'}): "
            f"{base_area.area:.1f} m^2, {_part_count(base_area)} part(s)")

    allowed: dict[str, BaseGeometry] = {pt: base_area for pt in plant_types}
    prohibited: list[ProhibitedZone] = []

    for rule in norms.rules:
        obstacle_geoms = groups.get((rule.obstacle_category, rule.obstacle_subtype), [])
        if not obstacle_geoms:
            continue

        for plant_type in plant_types:
            norm = rule.norm(plant_type)
            distance = norm.distance_m
            # Buffer each obstacle individually, THEN union the resulting
            # polygons -- buffering a single already-unioned geometry (e.g. a
            # MultiPoint of thousands of existing trees) is pathologically
            # slow in GEOS (~100x+ slower, observed >2 min vs ~2s on a
            # ~11.5k-point category); per-item buffer + cascaded union is the
            # standard workaround and is what actually scales here.
            buffer = unary_union([g.buffer(distance) for g in obstacle_geoms])
            allowed[plant_type] = allowed[plant_type].difference(buffer)

            report_geom = buffer.intersection(base_area)
            if report_geom.is_empty:
                continue
            if verbose:
                log(f"  [{rule.obstacle_category}/{rule.obstacle_subtype}] {plant_type}: "
                    f"{len(obstacle_geoms)} obstacle(s), {distance}m buffer -> "
                    f"{report_geom.area:.1f} m^2 prohibited (within base_area)")
            prohibited.append(
                ProhibitedZone(
                    geometry=report_geom,
                    plant_type=plant_type,
                    obstacle_category=rule.obstacle_category,
                    obstacle_subtype=rule.obstacle_subtype,
                    distance_m=distance,
                    citation=norm.citation,
                    norm_id=norm.id,
                    basis=norm.basis,
                    clause=norm.clause,
                    source_url=norm.source_url,
                    reason=f"< {distance} м от объекта типа «{rule.obstacle_subtype or rule.obstacle_category}»",
                )
            )

    if verbose:
        for plant_type in plant_types:
            a = allowed[plant_type]
            log(f"allowed[{plant_type}]: {a.area:.1f} m^2, {_part_count(a)} part(s)")

    uncovered = sorted(
        {
            key for key, geoms in groups.items()
            if geoms and key not in _STRUCTURAL_CATEGORIES
            and norms.find(key[0], key[1]) is None
        }
    )
    if uncovered:
        log(f"WARN: {len(uncovered)} category/subtype pair(s) present in the parsed data have "
            f"no matching setback norm and were NOT buffered (not treated as an obstacle): "
            f"{uncovered}")

    return ZoningResult(
        plant_types=list(plant_types),
        lawn_raw=lawn,
        site_boundary=site_boundary,
        base_area=base_area,
        used_site_boundary=used_site_boundary,
        allowed=allowed,
        prohibited=prohibited,
        uncovered_categories=uncovered,
    )
