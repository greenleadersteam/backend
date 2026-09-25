"""DXF entity -> shapely geometry conversion.

Ported from the original backend/dxf_to_geojson.py prototype (HATCH via
ezdxf.path.from_hatch, INSERT block explosion, near-closed-polyline
detection) with behavior preserved, plus polygon validity repair (drawings
routinely produce self-intersecting/degenerate rings -- e.g. a closed
LWPOLYLINE boundary loop -- that GEOS operations like unary_union/buffer
reject outright with a TopologyException; repairing at conversion time means
every downstream consumer gets a usable geometry instead of failing or
silently falling back to a LineString).
"""

from __future__ import annotations

import logging
import math
import sys

import ezdxf.path
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union
from shapely.validation import make_valid

logging.getLogger("ezdxf").setLevel(logging.ERROR)  # silence noisy "copy process ignored ..." logs

SAGITTA = 0.05  # max deviation (drawing units) when flattening arcs/splines to lines

INSUNITS_TO_METERS = {
    1: 0.0254,      # inches
    2: 0.3048,      # feet
    3: 1609.344,    # miles
    4: 0.001,       # millimeters
    5: 0.01,        # centimeters
    6: 1.0,         # meters
    7: 1000.0,      # kilometers
    10: 0.9144,     # yards
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def unit_scale_to_meters(doc) -> tuple[float, int]:
    code = doc.header.get("$INSUNITS", 0)
    factor = INSUNITS_TO_METERS.get(code)
    if factor is None:
        log(f"WARN: unsupported or unset $INSUNITS={code}, assuming values are already meters")
        factor = 1.0
    return factor, code


def flatten(entity):
    path = ezdxf.path.make_path(entity)
    return [(v.x, v.y) for v in path.flattening(SAGITTA)]


def is_effectively_closed(pts) -> bool:
    """Some drawings leave a polyline's closed-flag unset even though the first and
    last vertices coincide (or nearly do) by construction. Treat it as closed if the
    gap is tiny relative to the ring's own size, rather than trusting the flag alone.
    """
    if len(pts) < 4:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    diagonal = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5
    if diagonal == 0:
        return False
    gap = ((pts[0][0] - pts[-1][0]) ** 2 + (pts[0][1] - pts[-1][1]) ** 2) ** 0.5
    return gap <= diagonal * 1e-4


def repair_polygon(poly):
    """Return a valid Polygon/MultiPolygon equivalent to poly, or None if
    nothing polygonal survives repair (e.g. it collapses to a line/point).
    """
    if poly.is_valid:
        return poly if not poly.is_empty else None
    fixed = make_valid(poly)
    if fixed.geom_type in ("Polygon", "MultiPolygon") and not fixed.is_empty:
        return fixed
    if fixed.geom_type == "GeometryCollection":
        parts = [g for g in fixed.geoms if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
        if parts:
            return unary_union(parts)
    return None


def hatch_to_polygon(entity):
    rings = []
    for path in ezdxf.path.from_hatch(entity):
        pts = [(v.x, v.y) for v in path.flattening(SAGITTA)]
        if len(pts) >= 4:
            rings.append(pts)
    if not rings:
        return None
    polys = [Polygon(r) for r in rings]
    polys.sort(key=lambda p: p.area, reverse=True)
    exterior, rest = polys[0], polys[1:]
    holes = [list(p.exterior.coords) for p in rest if exterior.contains(p.representative_point())]
    try:
        poly = Polygon(exterior.exterior.coords, holes)
    except Exception:
        poly = exterior
    return repair_polygon(poly)


def entity_to_geometry(entity):
    t = entity.dxftype()
    try:
        if t == "INSERT":
            p = entity.dxf.insert
            return Point(p.x, p.y)
        if t == "POINT":
            p = entity.dxf.location
            return Point(p.x, p.y)
        if t in ("TEXT", "MTEXT"):
            # Both expose .dxf.insert (their anchor position) and
            # .plain_text() uniformly -- the text content itself is read
            # separately, by whoever needs it (see pipeline.py's geodetic
            # benchmark label extraction), not here.
            p = entity.dxf.insert
            return Point(p.x, p.y)
        if t == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            return LineString([(s.x, s.y), (e.x, e.y)])
        if t == "CIRCLE":
            pts = flatten(entity)
            return repair_polygon(Polygon(pts)) if len(pts) >= 4 else None
        if t == "ARC":
            pts = flatten(entity)
            return LineString(pts) if len(pts) >= 2 else None
        if t == "ELLIPSE":
            pts = flatten(entity)
            full = abs(entity.dxf.end_param - entity.dxf.start_param) >= 2 * math.pi - 1e-6
            if full and len(pts) >= 4:
                return repair_polygon(Polygon(pts))
            return LineString(pts) if len(pts) >= 2 else None
        if t in ("LWPOLYLINE", "POLYLINE", "SPLINE"):
            pts = flatten(entity)
            if len(pts) < 2:
                return None
            closed = False
            if t == "LWPOLYLINE":
                closed = bool(entity.closed)
            elif t == "POLYLINE":
                closed = bool(entity.is_closed)
            if not closed:
                closed = is_effectively_closed(pts)
            if closed and len(pts) >= 4:
                try:
                    poly = repair_polygon(Polygon(pts))
                    if poly is not None and poly.area > 0:
                        return poly
                except Exception:
                    pass
            return LineString(pts)
        if t == "HATCH":
            return hatch_to_polygon(entity)
    except Exception as ex:
        log(f"WARN: failed to convert {t} handle={entity.dxf.handle}: {ex}")
        return None
    return None  # unsupported entity type, silently skipped


def explode_insert(entity, max_depth: int = 5):
    """Yield the fully flattened, WCS-transformed leaf entities of a block reference,
    recursing into nested INSERTs (ezdxf's own virtual_entities() only expands one
    level). max_depth guards against pathological/cyclic block definitions.
    """
    if max_depth <= 0:
        return
    try:
        children = list(entity.virtual_entities())
    except Exception as ex:
        log(f"WARN: failed to explode INSERT handle={entity.dxf.handle}: {ex}")
        return
    for child in children:
        if child.dxftype() == "INSERT":
            yield from explode_insert(child, max_depth - 1)
        else:
            yield child


# Below this length/area, a part is almost certainly a decorative symbol-library
# artifact (a tick mark, a near-zero-length construction line) rather than real
# drawn geometry -- and worse than just noise, degenerate near-zero-length parts
# left inside a unioned MultiLineString have been observed to trigger a
# pathological GEOS buffer() slowdown (a single such part turned a 2s buffer
# into one that hadn't finished after 3+ minutes), so they're dropped here,
# at the source, rather than patched around in every later consumer.
_MIN_LINE_LENGTH = 1e-6  # meters
_MIN_POLYGON_AREA = 1e-9  # square meters


def _is_negligible(geom) -> bool:
    if geom.geom_type == "LineString":
        return geom.length < _MIN_LINE_LENGTH
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom.area < _MIN_POLYGON_AREA
    return False


def geometry_for_matched_entity(entity, expand_blocks: bool, rule_id: str):
    """Like entity_to_geometry, but honors expand_blocks for INSERT entities:
    explode into child geometry instead of collapsing to the insertion point.
    """
    if entity.dxftype() == "INSERT" and expand_blocks:
        parts = [
            g for child in explode_insert(entity)
            if (g := entity_to_geometry(child)) is not None and not g.is_empty and not _is_negligible(g)
        ]
        if parts:
            return unary_union(parts)
        log(f"WARN: INSERT handle={entity.dxf.handle} ({entity.dxf.name}) had no convertible "
            f"child geometry for req {rule_id}, falling back to insertion point")
    return entity_to_geometry(entity)


def scale_geometry(geom, factor: float):
    if factor == 1.0:
        return geom
    return shapely_transform(lambda x, y, z=None: (x * factor, y * factor), geom)
