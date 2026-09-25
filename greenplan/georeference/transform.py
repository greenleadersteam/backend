"""Fit a rigid (rotation + translation, scale locked to 1.0) transform from
local DXF drawing coordinates to real-world UTM coordinates, using geodetic
benchmark points labeled in the DXF (greenplan.georeference.points) matched
against geobridge.ru's point catalog (greenplan.georeference.geobridge).

Matching needs a caller-supplied approximate bbox (WGS84 lon/lat) of the
project site: geobridge's search is a country-wide substring match on
`title`, and catalog point numbers are NOT globally unique -- querying
"1763" returns 27 candidates across 8 regions, and even an exact title match
can collide: two different physical points are both titled "15521" in
Московская область alone, 13km apart (confirmed live, 2026-09-25). Without a
bbox to filter candidates against, there is no safe way to pick one -- a
label left with zero or more than one bbox-filtered candidate is treated as
unmatched rather than guessed, since fitting against one wrong point is far
worse than fitting with fewer points.

Validated precedent (Makeeva, 3 points, real geobridge-sourced coordinates):
a rigid transform (EuclideanTransform -- scale is fixed at 1.0 because that
class has no scale parameter, unlike SimilarityTransform; both sides are
already real physical meters, so letting scale float would just absorb
3-point noise into a parameter that should be exactly 1) gave 0.11-0.19m
residuals.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pyproj import Transformer
from shapely.geometry import Point
from skimage.transform import EuclideanTransform

from greenplan.georeference import geobridge
from greenplan.georeference.geobridge import GeobridgePoint
from greenplan.georeference.points import extract_labeled_points
from greenplan.model import FeatureCollection

UTM_CRS_DEFAULT = "EPSG:32637"  # UTM zone 37N -- covers Moscow, see CLAUDE.md
WGS84_CRS = "EPSG:4326"
MIN_MATCHED_POINTS_DEFAULT = 2
DEFAULT_RESIDUAL_THRESHOLD_M = 1.0


class InsufficientMatchedPointsError(Exception):
    """Fewer than min_matched_points labels resolved to an unambiguous
    real-world coordinate -- too few to fit even an unvalidated transform.
    """


class ResidualTooHighError(Exception):
    """3+ matched points fit a transform whose worst residual exceeds the
    threshold -- more likely a bad match (wrong candidate, wrong label) than
    real measurement noise -- and, if 4+ points were available, excluding the
    single best-candidate outlier (see _fit_best_single_exclusion) still
    didn't bring the remaining worst residual under the threshold either.
    """


@dataclass(frozen=True)
class GeoreferenceResult:
    local_to_utm: EuclideanTransform
    utm_to_local: EuclideanTransform
    utm_crs: str
    matched_labels: list[str]
    residuals_m: dict[str, float]
    confidence: str  # "validated" | "unvalidated"
    # {label: (local DXF-frame point, matched geobridge point)} -- kept around
    # (not just used transiently) so callers can export both reference-point
    # sources side by side for manual QA of the fit, see
    # export.georeference_points.georeference_points_to_geojson. Includes any
    # excluded point too (see excluded_label) so it stays visible for QA
    # rather than silently vanishing.
    matched_points: dict[str, tuple[Point, GeobridgePoint]]
    # Set only when a 4+-point fit initially failed the residual check and
    # excluding this one label (the best of all N single-exclusion candidates,
    # not just the full-fit's worst-residual point) brought the rest under
    # threshold. `excluded_residual_m` is that label's residual in the
    # *original, full-set* fit -- i.e. how bad it looked before being dropped.
    excluded_label: str | None = None
    excluded_residual_m: float | None = None


def _normalize(title: str) -> str:
    return title.strip().upper()


def match_candidate(
    label: str, candidates: list[GeobridgePoint], bbox: tuple[float, float, float, float]
) -> GeobridgePoint | None:
    """Exact title match within bbox; if that's empty, fall back to a
    conservative normalized (trimmed/uppercased) title match, still
    bbox-filtered. Zero or multiple survivors at either stage means no match
    -- see module docstring for why guessing is unsafe here.
    """
    minx, miny, maxx, maxy = bbox

    def in_bbox(c: GeobridgePoint) -> bool:
        return minx <= c.lng <= maxx and miny <= c.lat <= maxy

    exact = [c for c in candidates if c.title == label and in_bbox(c)]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        normalized = _normalize(label)
        fallback = [c for c in candidates if _normalize(c.title) == normalized and in_bbox(c)]
        if len(fallback) == 1:
            return fallback[0]
    return None


def _fit(
    matched_points: dict[str, tuple[Point, GeobridgePoint]],
    labels: list[str],
    utm_crs: str,
) -> tuple[EuclideanTransform, dict[str, float]] | None:
    """Fit a rigid transform on exactly this label subset and return
    (transform, {label: residual_m}), or None if the fit is degenerate
    (e.g. coincident points).
    """
    local_xy = np.array([[matched_points[label][0].x, matched_points[label][0].y] for label in labels])

    to_utm = Transformer.from_crs(WGS84_CRS, utm_crs, always_xy=True)
    utm_xy = np.array(
        [to_utm.transform(matched_points[label][1].lng, matched_points[label][1].lat) for label in labels]
    )

    tform = EuclideanTransform.from_estimate(local_xy, utm_xy)
    if not tform:
        return None

    predicted = tform(local_xy)
    residuals_m = {label: float(np.hypot(*(predicted[i] - utm_xy[i]))) for i, label in enumerate(labels)}
    return tform, residuals_m


def _fit_best_single_exclusion(
    matched_points: dict[str, tuple[Point, GeobridgePoint]],
    all_labels: list[str],
    utm_crs: str,
) -> tuple[str, EuclideanTransform, dict[str, float]] | None:
    """Try excluding each label once (leave-one-out, not just the full fit's
    worst-residual point -- see module docstring/CLAUDE.md for why a search
    over all candidates is more correct than assuming the naive worst point
    is necessarily the right one to drop). Returns whichever single exclusion
    minimizes the worst residual among the survivors, or None if every
    exclusion is degenerate.
    """
    best: tuple[str, EuclideanTransform, dict[str, float]] | None = None
    best_worst = float("inf")
    for candidate in all_labels:
        remaining = [label for label in all_labels if label != candidate]
        fit = _fit(matched_points, remaining, utm_crs)
        if fit is None:
            continue
        tform, residuals_m = fit
        worst = max(residuals_m.values())
        if worst < best_worst:
            best_worst = worst
            best = (candidate, tform, residuals_m)
    return best


def fit_rigid_transform(
    matched_points: dict[str, tuple[Point, GeobridgePoint]],
    *,
    utm_crs: str = UTM_CRS_DEFAULT,
    min_matched_points: int = MIN_MATCHED_POINTS_DEFAULT,
    residual_threshold_m: float = DEFAULT_RESIDUAL_THRESHOLD_M,
) -> GeoreferenceResult:
    """matched_points: {label: (local_point, geobridge_point)}.

    With 4+ points, a full-set fit whose worst residual exceeds the
    threshold gets one automatic retry: exclude whichever single label's
    absence brings the worst remaining residual down the most (see
    _fit_best_single_exclusion), and accept that instead if it now passes.
    Never excludes more than one point, and never attempts this at all with
    exactly 3 points, since excluding one of those would only leave 2 --
    downgrading to the weaker "unvalidated" tier instead of actually
    validating, which isn't a fair substitute for a failed check.
    """
    if len(matched_points) < min_matched_points:
        raise InsufficientMatchedPointsError(
            f"Only {len(matched_points)} geodetic point(s) matched a real-world "
            f"coordinate (need at least {min_matched_points})"
        )

    labels = sorted(matched_points)  # deterministic fit regardless of dict order
    fit = _fit(matched_points, labels, utm_crs)
    if fit is None:
        raise InsufficientMatchedPointsError(
            f"Could not fit a rigid transform from the {len(matched_points)} matched point(s) "
            f"(degenerate geometry, e.g. coincident points)"
        )
    local_to_utm, residuals_m = fit

    if len(labels) == 2:
        return GeoreferenceResult(
            local_to_utm=local_to_utm,
            utm_to_local=local_to_utm.inverse,
            utm_crs=utm_crs,
            matched_labels=labels,
            residuals_m=residuals_m,
            confidence="unvalidated",
            matched_points=matched_points,
        )

    worst = max(residuals_m.values())
    if worst <= residual_threshold_m:
        return GeoreferenceResult(
            local_to_utm=local_to_utm,
            utm_to_local=local_to_utm.inverse,
            utm_crs=utm_crs,
            matched_labels=labels,
            residuals_m=residuals_m,
            confidence="validated",
            matched_points=matched_points,
        )

    if len(labels) >= 4:
        best = _fit_best_single_exclusion(matched_points, labels, utm_crs)
        if best is not None:
            excluded_label, sub_tform, sub_residuals = best
            if max(sub_residuals.values()) <= residual_threshold_m:
                return GeoreferenceResult(
                    local_to_utm=sub_tform,
                    utm_to_local=sub_tform.inverse,
                    utm_crs=utm_crs,
                    matched_labels=[label for label in labels if label != excluded_label],
                    residuals_m=sub_residuals,
                    confidence="validated",
                    matched_points=matched_points,
                    excluded_label=excluded_label,
                    excluded_residual_m=residuals_m[excluded_label],
                )

    raise ResidualTooHighError(
        f"Worst residual {worst:.3f}m exceeds threshold {residual_threshold_m}m: {residuals_m}"
    )


def georeference(
    fc: FeatureCollection,
    bbox: tuple[float, float, float, float],
    *,
    client,
    timeout: float = geobridge.DEFAULT_TIMEOUT_S,
    base_url: str = geobridge.BASE_URL,
    utm_crs: str = UTM_CRS_DEFAULT,
    min_matched_points: int = MIN_MATCHED_POINTS_DEFAULT,
    residual_threshold_m: float = DEFAULT_RESIDUAL_THRESHOLD_M,
) -> GeoreferenceResult:
    """Extract labeled geodetic points from fc, look each up on geobridge,
    and fit a rigid local-to-UTM transform from the ones that match
    unambiguously (see match_candidate).
    """
    local_points = extract_labeled_points(fc)
    matched: dict[str, tuple[Point, GeobridgePoint]] = {}
    for label, point in local_points.items():
        candidates = geobridge.fetch_points(label, client=client, timeout=timeout, base_url=base_url)
        match = match_candidate(label, candidates, bbox)
        if match is not None:
            matched[label] = (point, match)
    return fit_rigid_transform(
        matched,
        utm_crs=utm_crs,
        min_matched_points=min_matched_points,
        residual_threshold_m=residual_threshold_m,
    )
