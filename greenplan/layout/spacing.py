"""Mutual-spacing helpers for layout placement.

Zoning already guarantees a candidate point respects every regulatory
setback (buffers are baked into the allowed-zone polygon). What zoning can't
encode is "at least N meters from every *other newly placed* plant of the
same type", since that depends on placement order -- so layout still needs
its own lightweight spacing check, ported from the prototype's nearest()/
thin() helpers.
"""

from __future__ import annotations

from shapely.geometry import Point
from shapely.strtree import STRtree


def min_distance(tree: STRtree, geoms: list[Point], pt: Point, max_check: float) -> float | None:
    """Exact minimum distance from pt to geoms, restricted via an STRtree
    query to candidates that could plausibly be within max_check -- makes
    checking against thousands of already-placed points tractable.
    """
    idxs = tree.query(pt.buffer(max_check))
    if len(idxs) == 0:
        return None
    return min(geoms[i].distance(pt) for i in idxs)


def thin(points: list[Point], spacing: float) -> list[Point]:
    """Greedily keep points that are at least `spacing` apart from every
    already-accepted point, in input order.
    """
    accepted: list[Point] = []
    for pt in points:
        if all(pt.distance(a) >= spacing for a in accepted):
            accepted.append(pt)
    return accepted
