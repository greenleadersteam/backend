"""Extract {label: local Point} pairs from a parsed FeatureCollection's
geodetic benchmark points (see rules/default.yaml's `geodetic_points` rule
and pipeline.py's extra["label"] extraction).
"""

from __future__ import annotations

from shapely.geometry import Point

from greenplan.model import FeatureCollection

# Confirmed junk labels across the pilot dataset (geodetic_points_inventory.md):
# "МЕЖ." is a boundary-marker label, not a benchmark catalog number; "Н/Н"
# ("нет номера") is a placeholder for a point with no assigned number at all.
# Neither is a real ID a real-coordinate lookup could ever resolve.
_JUNK_LABELS = {"МЕЖ.", "Н/Н"}


def extract_labeled_points(fc: FeatureCollection) -> dict[str, Point]:
    """Return {label: Point}, deduped by label (first occurrence wins -- the
    same physical benchmark routinely reappears across overlapping xref
    tiles) and filtered of junk/empty labels.
    """
    points: dict[str, Point] = {}
    for f in fc.features:
        if f.category != "geodetic_points":
            continue
        label = f.extra.get("label")
        if not label:
            continue
        label = label.strip()
        if not label or label in _JUNK_LABELS:
            continue
        if label in points:
            continue
        if f.geometry.geom_type != "Point":
            continue
        points[label] = f.geometry
    return points
