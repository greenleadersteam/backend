"""Reconstruct a clean polygon-with-holes topology from messy CAD line/polygon
soup: real drawings routinely represent one conceptual boundary as several
independently-drawn pieces (a big outline split across multiple entities
whose shared vertices don't land on exactly the same coordinates, plus
smaller loops meant as holes), and getting from that to a single "the
plantable area" polygon needs two distinct repairs.
"""

from __future__ import annotations

import math
from collections import defaultdict

from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from greenplan.geometry import repair_polygon

# Two polygons whose intersection-over-union is at least this similar are
# treated as the same real-world feature digitized twice (observed: 4
# copies of one site boundary and 6 copies each of its 8 holes, all at
# IoU > 0.9999, from a source folder that bundled 3 duplicate delivery
# packages). 0.98 comfortably separates "same feature, minor digitization
# noise" from "a real hole that just happens to be large relative to its
# exterior" (a real hole is practically never >90% of its exterior's area).
_DUPLICATE_IOU_THRESHOLD = 0.98

# A "hole" spanning almost the entire area of its supposed exterior is a red
# flag, not a real hole: it's what you get when a source folder bundles
# near-duplicate copies of the same boundary (e.g. the same site outline
# digitized twice across sibling delivery packages) and the two copies
# happen to differ just enough that one "mostly contains" the other. Treating
# that pair as outer+hole produces a degenerate sliver-thin annulus, which is
# exactly the kind of shape GEOS's unary_union rejects with a
# TopologyException ("side location conflict"). Requiring the inner polygon
# to be meaningfully smaller rules this out while leaving real holes (which
# are practically always a small fraction of their exterior's area) untouched.
_MAX_HOLE_TO_EXTERIOR_AREA_RATIO = 0.95

# A big boundary drawn/edited across multiple entities routinely has small
# gaps at the seams between them (observed: 1.09m and 0.06m on one real
# object's site-boundary layer, split across two LWPOLYLINE entities that
# otherwise trace one continuous outline). shapely.ops.polygonize() requires
# *exact* coordinate coincidence to link line endpoints into a ring, so any
# gap at all makes it silently drop the whole (large!) fragment rather than
# raise an error. 1.5m comfortably closes gaps of that size without being so
# large it would plausibly bridge two genuinely unrelated line fragments.
DEFAULT_ENDPOINT_SNAP_TOLERANCE_M = 1.5


def close_line_soup(lines: list[BaseGeometry], tolerance: float = DEFAULT_ENDPOINT_SNAP_TOLERANCE_M) -> list[Polygon]:
    """Snap nearby line endpoints together, then polygonize into closed rings.

    Endpoints within `tolerance` of each other (across different lines --
    a line's own two ends are never merged with each other) are clustered
    and moved to their cluster's centroid, so fragments that were meant to
    connect end-to-end actually do.
    """
    simple_lines: list[LineString] = []
    for g in lines:
        if g.geom_type == "LineString":
            simple_lines.append(g)
        elif g.geom_type == "MultiLineString":
            simple_lines.extend(g.geoms)

    if not simple_lines:
        return []

    # (line_index, coords) for the two endpoints of each line, in order.
    endpoints: list[tuple[int, tuple[float, float]]] = []
    for i, ln in enumerate(simple_lines):
        coords = list(ln.coords)
        endpoints.append((i, coords[0]))
        endpoints.append((i, coords[-1]))

    parent = list(range(len(endpoints)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a in range(len(endpoints)):
        for b in range(a + 1, len(endpoints)):
            if endpoints[a][0] == endpoints[b][0]:
                continue  # don't fuse a line's own two ends together
            if math.dist(endpoints[a][1], endpoints[b][1]) <= tolerance:
                union(a, b)

    clusters: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(endpoints)):
        clusters[find(idx)].append(idx)

    snapped_point: dict[int, tuple[float, float]] = {}
    for members in clusters.values():
        pts = [endpoints[m][1] for m in members]
        centroid = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        for m in members:
            snapped_point[m] = centroid

    snapped_lines = []
    for i, ln in enumerate(simple_lines):
        coords = list(ln.coords)
        coords[0] = snapped_point[2 * i]
        coords[-1] = snapped_point[2 * i + 1]
        snapped = LineString(coords)
        # Snapping can occasionally collapse a short line's two ends into the
        # same cluster (both within tolerance of each other and of the same
        # neighboring point), leaving a zero-length "line" that polygonize()
        # chokes on (observed as a `RuntimeWarning: invalid value encountered
        # in polygonize`, not just a silently-dropped fragment). It carries no
        # shape information either way, so drop it rather than feed it in.
        if snapped.length > 1e-9:
            snapped_lines.append(snapped)

    return [p for p in polygonize(snapped_lines) if not p.is_empty]


def _flatten_polygons(geoms: list[BaseGeometry]) -> list[Polygon]:
    flat = []
    for g in geoms:
        if g.geom_type == "Polygon" and not g.is_empty:
            flat.append(g)
        elif g.geom_type == "MultiPolygon":
            flat.extend(p for p in g.geoms if not p.is_empty)
    return flat


def _dedupe_near_identical(polys: list[Polygon]) -> list[Polygon]:
    """Collapse groups of near-identical polygons (IoU >=
    _DUPLICATE_IOU_THRESHOLD) into a single representative each.

    Without this, a duplicated source folder doesn't just waste work: it
    actively corrupts the nesting reconstruction below -- multiple copies of
    the same real hole become multiple overlapping "hole" rings that make
    Polygon() construction invalid, and multiple copies of the same real
    exterior become separate top-level roots that, once unioned back
    together at the end, silently re-fill any holes a single copy had
    correctly carved out. Uses an STRtree so this stays cheap even for a
    category with hundreds of polygons (e.g. lawn), where actual duplicates
    are rare and most pairs can be bbox-rejected without a real intersection
    test.
    """
    n = len(polys)
    if n <= 1:
        return polys

    tree = STRtree(polys)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in tree.query(polys[i]):
            j = int(j)
            if j <= i:
                continue
            inter = polys[i].intersection(polys[j]).area
            if inter == 0:
                continue
            union_area = polys[i].area + polys[j].area - inter
            if union_area and inter / union_area >= _DUPLICATE_IOU_THRESHOLD:
                union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    return [max((polys[m] for m in members), key=lambda p: p.area) for members in groups.values()]


def resolve_nesting(geoms: list[BaseGeometry]) -> BaseGeometry | None:
    """Combine simple polygons into a single polygon-with-holes topology
    using even-odd nesting: a polygon fully (or almost fully -- real CAD
    boundaries rarely nest with pixel-perfect exactness) contained within
    another is a hole of its immediate (smallest) enclosing polygon; a
    polygon nested one level deeper than that -- inside a hole -- is solid
    again (an island in the hole), and so on.

    Degrades to a plain union when nothing is actually nested, so this is
    safe to apply generically (lawn patches, which are rarely nested in each
    other, as well as boundaries, which routinely are).
    """
    # Repair defensively before anything else: a polygon coming out of
    # close_line_soup's polygonize (or a source polygon that was already
    # borderline-invalid) can make every later containment check and union
    # in this function unpredictable, or make GEOS reject the final union
    # outright with a TopologyException.
    polys = [repair_polygon(p) for p in _flatten_polygons(geoms)]
    polys = _flatten_polygons([p for p in polys if p is not None])
    if not polys:
        return None
    polys = _dedupe_near_identical(polys)
    if len(polys) == 1:
        return polys[0]

    # Largest first, so "does j contain i" only ever needs checking against
    # already-seen, necessarily-larger candidates.
    polys = sorted(polys, key=lambda p: p.area, reverse=True)
    n = len(polys)
    parent: list[int | None] = [None] * n
    for i in range(n):
        best: int | None = None
        for j in range(i):  # all j before i are >= area, candidates for parent
            if polys[i].area > polys[j].area * _MAX_HOLE_TO_EXTERIOR_AREA_RATIO:
                continue  # too close in size to be a real hole -- likely a near-duplicate
            if _mostly_contains(polys[j], polys[i]) and (best is None or polys[j].area < polys[best].area):
                best = j
        parent[i] = best

    children: dict[int, list[int]] = defaultdict(list)
    roots = []
    for i, p in enumerate(parent):
        (children[p] if p is not None else roots).append(i)

    def build(i: int) -> BaseGeometry:
        child_indices = children.get(i, [])
        islands = []
        holes = []
        if child_indices:
            # Children of the same parent can include several near-duplicate
            # copies of the *same* real hole (routine when a source folder
            # bundles duplicate delivery packages, as here -- one hole ended
            # up with 6 near-identical copies). Feeding that many
            # overlapping rings straight into Polygon() as separate holes
            # produces an invalid polygon that repair can't recover holes
            # from at all (observed: it silently fell back to "no holes").
            # Unioning the children first collapses duplicates into one
            # shape and leaves genuinely distinct, disjoint holes as
            # separate connected components either way -- correct in both
            # cases, not just the duplicate one.
            child_union = _safe_union([polys[c] for c in child_indices])
            holes = [list(part.exterior.coords) for part in _flatten_polygons([child_union])]
            for c in child_indices:
                for grandchild in children.get(c, []):
                    islands.append(build(grandchild))
        solid = Polygon(polys[i].exterior.coords, holes)
        repaired = repair_polygon(solid)
        solid = repaired if repaired is not None else Polygon(polys[i].exterior.coords)
        return _safe_union([solid, *islands]) if islands else solid

    parts = [build(i) for i in roots]
    return _safe_union(parts) if len(parts) > 1 else parts[0]


def _safe_union(geoms: list[BaseGeometry]) -> BaseGeometry:
    """unary_union, but repairing each input first -- a plain unary_union
    raises a hard GEOSException on invalid input (unlike e.g. buffer, which
    tends to tolerate it), so anything reaching this point needs to already
    be valid.
    """
    repaired = [repair_polygon(g) if g.geom_type in ("Polygon", "MultiPolygon") else g for g in geoms]
    return unary_union([g for g in repaired if g is not None])


def _mostly_contains(outer: Polygon, inner: Polygon, min_overlap_ratio: float = 0.99) -> bool:
    if outer.contains(inner):
        return True
    if inner.area == 0:
        return False
    return outer.intersection(inner).area / inner.area >= min_overlap_ratio
