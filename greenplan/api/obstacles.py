"""`processed/obstacles.geojson`: the part of `parsed.geojson` the frontend
draws and measures planting distances against, for `GET /obstacles`.

`parsed.geojson` is every recognised entity -- 20-150 MB on real objects,
mostly contours, lawn hatches and survey detail no norm applies to. This
keeps only:
- features with a setback norm (the same exact (category, subtype) match
  zoning uses), plus the site boundary;
- once zoning has run, within its `base_area` (lawn ∩ site boundary) plus
  a margin -- survey sheets often cover far more than the work area (bbox
  test, so a long line crossing the extent is kept whole). Not the raw
  site_boundary features: real ones include stray stubs kilometres away,
  sometimes with no real boundary at all. Before zoning (a project that
  failed at georeferencing) nothing is clipped.

It also rounds coordinates (1 cm), drops server paths
(`metadata.root_file`/`source_folder`, `properties.source_file`), and
splits GeometryCollection/MultiPoint into one feature per part: the
contract (and the frontend) only knows Point, (Multi)LineString and
(Multi)Polygon.

Plain dicts only: the API process builds this lazily for projects processed
before it existed, and must not import the geo stack for that.
"""

from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

from greenplan.norms.schema import NormsTable

PARSED_FILENAME = "parsed.geojson"
OBSTACLES_FILENAME = "obstacles.geojson"
ZONES_FILENAME = "zones.geojson"

_EXTRA_CATEGORIES = {"site_boundary"}
_DROPPED_METADATA = ("root_file", "source_folder")
_DROPPED_PROPERTIES = ("source_file",)

# Obstacles this far outside the site extent are kept: beyond the largest
# setback that matters in practice (school 10 m; frontend looks up to 3x the
# largest) plus overhead power-line zones up to 55 m.
EXTENT_MARGIN_M = 60.0
_METERS_PER_DEGREE = 111_320.0

_lazy_build_lock = threading.Lock()

Bbox = tuple[float, float, float, float]


def obstacles_geojson(parsed: dict, norms: NormsTable, extent: Bbox | None = None) -> dict:
    """`extent`: bbox (in parsed's own CRS) to clip to, before the margin.
    Idempotent: re-running it on its own output only applies a new extent."""
    metadata = {k: v for k, v in parsed.get("metadata", {}).items() if k not in _DROPPED_METADATA}
    geographic = str(metadata.get("crs", "")).startswith("EPSG:4326")
    ndigits = 7 if geographic else 2  # ~1 cm either way
    if extent is not None:
        extent = _expand(extent, EXTENT_MARGIN_M, geographic)

    covered = {(r.obstacle_category, r.obstacle_subtype) for r in norms.rules}
    features = []
    for feature in parsed["features"]:
        props = feature["properties"]
        category = props.get("category")
        if category not in _EXTRA_CATEGORIES and (category, props.get("subtype")) not in covered:
            continue
        properties = {k: v for k, v in props.items() if k not in _DROPPED_PROPERTIES}
        for geometry in _simple_parts(feature["geometry"]):
            box = _bbox(geometry)
            if box is None or (extent is not None and not _intersects(box, extent)):
                continue
            geometry = {"type": geometry["type"], "coordinates": _round(geometry["coordinates"], ndigits)}
            features.append({"type": "Feature", "geometry": geometry, "properties": properties})
    return {"type": "FeatureCollection", "metadata": metadata, "features": features}


def extent_from_zones(zones: dict) -> Bbox | None:
    """bbox of zones.geojson's base_area feature, if any."""
    for feature in zones["features"]:
        if feature["properties"].get("zone_type") == "base_area":
            return _bbox(feature["geometry"])
    return None


def _simple_parts(geometry: dict) -> list[dict]:
    if geometry["type"] == "GeometryCollection":
        return [part for g in geometry["geometries"] for part in _simple_parts(g)]
    if geometry["type"] == "MultiPoint":
        return [{"type": "Point", "coordinates": c} for c in geometry["coordinates"]]
    return [geometry]


def _positions(coordinates):
    if coordinates and isinstance(coordinates[0], (int, float)):
        yield coordinates
    else:
        for c in coordinates:
            yield from _positions(c)


def _bbox(geometry: dict) -> Bbox | None:
    xs, ys = [], []
    for position in _positions(geometry["coordinates"]):
        xs.append(position[0])
        ys.append(position[1])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def _round(coordinates, ndigits: int):
    if isinstance(coordinates, (int, float)):
        return round(coordinates, ndigits)
    return [_round(c, ndigits) for c in coordinates]


def _intersects(a: Bbox, b: Bbox) -> bool:
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _expand(box: Bbox, meters: float, geographic: bool) -> Bbox:
    if geographic:
        dy = meters / _METERS_PER_DEGREE
        dx = dy / max(math.cos(math.radians((box[1] + box[3]) / 2)), 1e-6)
    else:
        dx = dy = meters
    return (box[0] - dx, box[1] - dy, box[2] + dx, box[3] + dy)


def write_obstacles(processed_dir: Path, obstacles: dict) -> None:
    _atomic_write_text(processed_dir / OBSTACLES_FILENAME, json.dumps(obstacles, ensure_ascii=False))


def ensure_obstacles(processed_dir: Path) -> Path | None:
    """Path of obstacles.geojson, built from parsed.geojson (clipped by
    zones.geojson, if any) first for a project processed before it existed.
    None if there's no parsed.geojson either.
    """
    path = processed_dir / OBSTACLES_FILENAME
    if not path.is_file():
        with _lazy_build_lock:
            if not path.is_file():
                parsed_path = processed_dir / PARSED_FILENAME
                if not parsed_path.is_file():
                    return None
                parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
                zones_path = processed_dir / ZONES_FILENAME
                extent = (
                    extent_from_zones(json.loads(zones_path.read_text(encoding="utf-8")))
                    if zones_path.is_file() else None
                )
                write_obstacles(processed_dir, obstacles_geojson(parsed, NormsTable.load(), extent))
    return path


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
