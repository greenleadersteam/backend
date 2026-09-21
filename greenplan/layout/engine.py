"""Place new trees/shrubs inside the allowed zone, following the declarative
PlantingRuleSet. Ported from the prototype's full4_run.py (row/hedge along
curbs, tangent + perpendicular-offset) and full5_fill.py/full6_shrubfill.py
(grid fill), simplified: since the allowed-zone polygon already encodes every
regulatory setback (see greenplan.zoning), a candidate only needs a single
`allowed.contains()` check plus a mutual-spacing check against points already
placed by earlier rules of the same plant type (see greenplan.layout.spacing).
"""

from __future__ import annotations

import math
import sys
from itertools import count

from shapely.geometry import LineString, Point
from shapely.prepared import prep
from shapely.strtree import STRtree

from greenplan.layout.rules import PlantingRule, PlantingRuleSet
from greenplan.layout.spacing import min_distance, thin
from greenplan.model import FeatureCollection, PlantingPoint, ZoningResult


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _curb_linestrings(fc: FeatureCollection) -> list[LineString]:
    lines: list[LineString] = []
    for f in fc.features:
        if f.category != "road_edge":
            continue
        geom = f.geometry
        if geom.geom_type == "LineString":
            lines.append(geom)
        elif geom.geom_type == "MultiLineString":
            lines.extend(geom.geoms)
        elif geom.geom_type == "Polygon":
            lines.append(LineString(geom.exterior.coords))
            lines.extend(LineString(ring.coords) for ring in geom.interiors)
        elif geom.geom_type == "MultiPolygon":
            for poly in geom.geoms:
                lines.append(LineString(poly.exterior.coords))
                lines.extend(LineString(ring.coords) for ring in poly.interiors)
    return [line for line in lines if line.length > 0]


def _row_along_curb(
    curbs, allowed_prepared, occupied_tree, occupied_pts, rule: PlantingRule, verbose: bool = False
) -> list[Point]:
    candidates: list[Point] = []
    sample_positions = 0
    inside_allowed = 0
    passed_spacing = 0
    for curb in curbs:
        length = curb.length
        if length == 0:
            continue
        steps = max(1, int(length // rule.spacing_m))
        for i in range(steps + 1):
            sample_positions += 1
            s = min(i * rule.spacing_m, length)
            base = curb.interpolate(s)
            p2 = curb.interpolate(min(s + 0.5, length))
            dx, dy = p2.x - base.x, p2.y - base.y
            norm = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / norm, dx / norm
            for side in (1, -1):
                cand = Point(base.x + nx * side * rule.offset_m, base.y + ny * side * rule.offset_m)
                if not allowed_prepared.contains(cand):
                    continue
                inside_allowed += 1
                if occupied_tree is not None:
                    dmin = min_distance(occupied_tree, occupied_pts, cand, rule.spacing_m)
                    if dmin is not None and dmin < rule.spacing_m:
                        continue
                passed_spacing += 1
                candidates.append(cand)
                break
    thinned = thin(candidates, rule.spacing_m * 0.85)
    if verbose:
        log(f"  [{rule.id}] curb scan: {len(curbs)} curb line(s), {sample_positions} sample position(s), "
            f"{inside_allowed} side(s) inside allowed, {passed_spacing} passed occupied-spacing check, "
            f"{len(thinned)} survived self-thinning")
    return thinned


def _grid_fill(
    allowed_geom, allowed_prepared, occupied_tree, occupied_pts, rule: PlantingRule, verbose: bool = False
) -> list[Point]:
    minx, miny, maxx, maxy = allowed_geom.bounds
    candidates: list[Point] = []
    grid_points = 0
    inside_allowed = 0
    passed_spacing = 0
    x = minx
    while x <= maxx:
        y = miny
        while y <= maxy:
            grid_points += 1
            pt = Point(x, y)
            if allowed_prepared.contains(pt):
                inside_allowed += 1
                ok = True
                if occupied_tree is not None:
                    dmin = min_distance(occupied_tree, occupied_pts, pt, rule.spacing_m)
                    if dmin is not None and dmin < rule.spacing_m:
                        ok = False
                if ok:
                    passed_spacing += 1
                    candidates.append(pt)
            y += rule.spacing_m
        x += rule.spacing_m
    thinned = thin(candidates, rule.spacing_m - 0.01)
    if verbose:
        log(f"  [{rule.id}] grid scan: bbox {minx:.1f},{miny:.1f} - {maxx:.1f},{maxy:.1f}, "
            f"{grid_points} grid point(s), {inside_allowed} inside allowed, "
            f"{passed_spacing} passed occupied-spacing check, {len(thinned)} survived self-thinning")
    return thinned


def generate_layout(
    zoning: ZoningResult,
    fc: FeatureCollection,
    planting_rules: PlantingRuleSet,
    verbose: bool = False,
) -> list[PlantingPoint]:
    curbs = _curb_linestrings(fc)
    if not curbs:
        log("WARN: no curb/kerb geometry found -- row/hedge placement rules will produce nothing")
    elif verbose:
        log(f"Curbs: {len(curbs)} line(s), {sum(c.length for c in curbs):.1f} m total length")

    placed_by_type: dict[str, list[Point]] = {pt: [] for pt in zoning.plant_types}
    results: list[PlantingPoint] = []
    counters: dict[str, count] = {}

    for rule in planting_rules.rules:
        if rule.plant_type not in zoning.allowed:
            log(f"WARN: planting rule {rule.id} targets plant_type={rule.plant_type!r}, which "
                f"zoning has no allowed-zone for; skipping")
            continue

        allowed_geom = zoning.allowed[rule.plant_type]
        if allowed_geom.is_empty:
            if verbose:
                log(f"  [{rule.id}] allowed area for {rule.plant_type!r} is empty, skipping")
            continue
        allowed_prepared = prep(allowed_geom)

        occupied_pts = placed_by_type[rule.plant_type]
        occupied_tree = STRtree(occupied_pts) if occupied_pts else None

        if rule.placement == "row_along_curb":
            new_points = _row_along_curb(
                curbs, allowed_prepared, occupied_tree, occupied_pts, rule, verbose=verbose
            )
        elif rule.placement == "grid_fill":
            new_points = _grid_fill(
                allowed_geom, allowed_prepared, occupied_tree, occupied_pts, rule, verbose=verbose
            )
        else:
            log(f"WARN: unknown placement kind {rule.placement!r} for rule {rule.id}, skipping")
            continue

        counter = counters.setdefault(rule.id, count(1))
        for pt in new_points:
            results.append(
                PlantingPoint(
                    id=f"{rule.id}-{next(counter):05d}",
                    geometry=pt,
                    plant_type=rule.plant_type,
                    rule_id=rule.id,
                )
            )
        placed_by_type[rule.plant_type].extend(new_points)

    return results
