import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import Point

from greenplan.georeference import geobridge as geobridge_module
from greenplan.georeference.apply import (
    euclidean_transform_fn,
    reproject_fn,
    transform_feature_collection,
    transform_planting_points,
)
from greenplan.georeference.geobridge import GeobridgePoint
from greenplan.georeference.points import extract_labeled_points
from greenplan.georeference.transform import (
    InsufficientMatchedPointsError,
    ResidualTooHighError,
    fit_rigid_transform,
    georeference,
    match_candidate,
)
from greenplan.model import Feature, FeatureCollection, PlantingPoint
from skimage.transform import EuclideanTransform

UTM_CRS = "EPSG:32637"
BBOX = (37.0, 55.0, 38.0, 56.0)  # covers the fake Moscow-area coordinates used below


def _fc(features: list[Feature]) -> FeatureCollection:
    return FeatureCollection(
        root_file="root.dxf", source_folder=".", insunits_code=6, scale_to_meters=1.0, features=features
    )


def _geo_feature(label: str, x: float, y: float) -> Feature:
    return Feature(
        geometry=Point(x, y),
        category="geodetic_points",
        rule_id="90",
        status="auto",
        layer="Геодезические пункты",
        dxftype="TEXT",
        source_file="root.dxf",
        extra={"label": label},
    )


# --- points.py: label extraction --------------------------------------------------


def test_extract_labeled_points_dedupes_and_filters_junk():
    fc = _fc(
        [
            _geo_feature("111", 0, 0),
            _geo_feature("111", 5, 5),  # duplicate label -- first occurrence wins
            _geo_feature("МЕЖ.", 1, 1),  # boundary-marker placeholder, not a real ID
            _geo_feature("Н/Н", 2, 2),  # "нет номера" placeholder
            _geo_feature("", 3, 3),  # empty label
            _geo_feature("222", 10, 10),
        ]
    )

    points = extract_labeled_points(fc)

    assert set(points) == {"111", "222"}
    assert points["111"] == Point(0, 0)


def test_extract_labeled_points_ignores_non_geodetic_and_unlabeled_features():
    other_category = Feature(
        geometry=Point(0, 0), category="buildings", rule_id="4", status="auto",
        layer="Здания", dxftype="TEXT", source_file="root.dxf", extra={"label": "111"},
    )
    no_label = Feature(
        geometry=Point(1, 1), category="geodetic_points", rule_id="90", status="auto",
        layer="Геодезические пункты", dxftype="LINE", source_file="root.dxf",
    )

    assert extract_labeled_points(_fc([other_category, no_label])) == {}


# --- transform.py: candidate matching ----------------------------------------------


def test_match_candidate_exact_within_bbox():
    candidates = [GeobridgePoint(title="111", lat=55.5, lng=37.5, region="50 - Московская область")]
    assert match_candidate("111", candidates, BBOX) is candidates[0]


def test_match_candidate_outside_bbox_is_excluded():
    candidates = [GeobridgePoint(title="111", lat=59.9, lng=30.5, region="47 - Ленинградская область")]
    assert match_candidate("111", candidates, BBOX) is None


def test_match_candidate_ambiguous_exact_matches_returns_none():
    """Two different real points can share a title within the same bbox --
    confirmed live against geobridge.ru (see transform.py's module docstring).
    Guessing between them is unsafe, so this must be treated as no match.
    """
    candidates = [
        GeobridgePoint(title="111", lat=55.5, lng=37.5, region="A"),
        GeobridgePoint(title="111", lat=55.6, lng=37.6, region="A"),
    ]
    assert match_candidate("111", candidates, BBOX) is None


def test_match_candidate_normalized_fallback():
    candidates = [GeobridgePoint(title=" 111 ", lat=55.5, lng=37.5, region="A")]
    assert match_candidate("111", candidates, BBOX) is candidates[0]


def test_match_candidate_no_candidates_returns_none():
    assert match_candidate("999", [], BBOX) is None


# --- transform.py: rigid-fit correctness / thresholds ------------------------------


def _matched_from_utm_pairs(pairs: list[tuple[tuple[float, float], tuple[float, float]]]) -> dict:
    to_wgs84 = Transformer.from_crs(UTM_CRS, "EPSG:4326", always_xy=True)
    matched = {}
    for i, (local_xy, utm_xy) in enumerate(pairs):
        lng, lat = to_wgs84.transform(*utm_xy)
        label = f"p{i}"
        matched[label] = (Point(*local_xy), GeobridgePoint(title=label, lat=lat, lng=lng, region="Московская область"))
    return matched


def test_fit_rigid_transform_below_minimum_raises():
    matched = _matched_from_utm_pairs([((0, 0), (400000.0, 6170000.0))])
    with pytest.raises(InsufficientMatchedPointsError):
        fit_rigid_transform(matched)


def test_fit_rigid_transform_exactly_two_points_is_unvalidated_regardless_of_fit():
    matched = _matched_from_utm_pairs([((0, 0), (400000.0, 6170000.0)), ((100, 0), (400100.0, 6170000.0))])

    result = fit_rigid_transform(matched)

    assert result.confidence == "unvalidated"
    assert set(result.matched_labels) == {"p0", "p1"}


def _exact_rigid_pairs(local_pts, rotation_deg, translation):
    theta = np.radians(rotation_deg)
    rot = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    pairs = []
    for lx, ly in local_pts:
        utm_xy = rot @ np.array([lx, ly]) + np.array(translation)
        pairs.append(((lx, ly), tuple(utm_xy)))
    return pairs


def test_fit_rigid_transform_three_points_exact_fit_is_validated_with_near_zero_residuals():
    pairs = _exact_rigid_pairs([(0, 0), (50, 0), (0, 80)], rotation_deg=30, translation=(400000.0, 6170000.0))
    matched = _matched_from_utm_pairs(pairs)

    result = fit_rigid_transform(matched, residual_threshold_m=0.5)

    assert result.confidence == "validated"
    assert all(r < 0.01 for r in result.residuals_m.values())


def test_fit_rigid_transform_outlier_point_exceeds_threshold_raises():
    pairs = _exact_rigid_pairs([(0, 0), (50, 0), (0, 80)], rotation_deg=30, translation=(400000.0, 6170000.0))
    lx, ly_utm = pairs[2]
    pairs[2] = (lx, (ly_utm[0] + 500.0, ly_utm[1]))  # 500m outlier on the third point

    matched = _matched_from_utm_pairs(pairs)

    with pytest.raises(ResidualTooHighError):
        fit_rigid_transform(matched, residual_threshold_m=1.0)


# --- transform.py: top-level georeference() orchestration --------------------------


def test_georeference_matches_labels_and_fits(monkeypatch):
    coords = {"111": (55.70, 37.60), "222": (55.71, 37.62)}

    def fake_fetch(label, *, client, timeout=10.0, base_url=None):
        if label not in coords:
            return []
        lat, lng = coords[label]
        return [GeobridgePoint(title=label, lat=lat, lng=lng, region="50 - Московская область")]

    monkeypatch.setattr(geobridge_module, "fetch_points", fake_fetch)
    fc = _fc([_geo_feature("111", 0, 0), _geo_feature("222", 100, 0)])

    result = georeference(fc, BBOX, client=object())

    assert result.confidence == "unvalidated"
    assert set(result.matched_labels) == {"111", "222"}


def test_georeference_raises_when_too_few_labels_match(monkeypatch):
    monkeypatch.setattr(geobridge_module, "fetch_points", lambda label, **kwargs: [])
    fc = _fc([_geo_feature("111", 0, 0)])

    with pytest.raises(InsufficientMatchedPointsError):
        georeference(fc, BBOX, client=object())


# --- apply.py: transform application ------------------------------------------------


def test_euclidean_transform_fn_applies_rigid_transform_to_geometries():
    local_pts = [(0, 0), (10, 0)]
    utm_pts = [(400000.0, 6170000.0), (400010.0, 6170000.0)]
    tform = EuclideanTransform.from_estimate(np.array(local_pts), np.array(utm_pts))

    fc = _fc([_geo_feature("111", 0, 0)])
    transformed = transform_feature_collection(fc, euclidean_transform_fn(tform))

    assert transformed.features[0].geometry.x == pytest.approx(400000.0, abs=1e-6)
    assert transformed.features[0].geometry.y == pytest.approx(6170000.0, abs=1e-6)
    # unrelated fields are preserved
    assert transformed.root_file == fc.root_file


def test_reproject_fn_round_trips_through_wgs84():
    to_wgs84 = reproject_fn(UTM_CRS)
    back_to_utm = reproject_fn("EPSG:4326", UTM_CRS)
    points = [PlantingPoint(id="1", geometry=Point(400000.0, 6170000.0), plant_type="tree", rule_id="1")]

    roundtripped = transform_planting_points(transform_planting_points(points, to_wgs84), back_to_utm)

    assert roundtripped[0].geometry.x == pytest.approx(400000.0, abs=1e-3)
    assert roundtripped[0].geometry.y == pytest.approx(6170000.0, abs=1e-3)
