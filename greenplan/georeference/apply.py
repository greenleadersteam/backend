"""Apply a fitted georeference transform (or a plain CRS reprojection) to
pipeline data structures.

The rigid transform from transform.py is used in two directions:
  - forward (local -> UTM): applied to the whole parsed FeatureCollection
    before zoning/layout run, so all setback/spacing math happens in real
    UTM meters instead of an arbitrary local-drawing frame. Zero changes are
    needed inside zoning/engine.py or layout/engine.py for this: every
    distance there (setback buffers, mutual spacing, close_line_soup's
    tolerance, the 1cm snap grid) is translation/rotation-invariant, so a
    rigid transform only changes the numbers' absolute magnitude, not the
    geometry relationships those modules reason about.
  - inverse (UTM -> local): applied to the resulting PlantingPoints right
    before DXF export, since dxf_sink.py adds geometry to the *existing*
    root drawing and so must stay in that drawing's own local frame.

A separate plain WGS84 reprojection (no rigid-fit math, just a CRS
transform) is applied to UTM-frame results right before GeoJSON export.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from pyproj import Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from skimage.transform import EuclideanTransform

from greenplan.model import FeatureCollection, PlantingPoint, ZoningResult

WGS84_CRS = "EPSG:4326"
WGS84_CRS_LABEL = "EPSG:4326 (WGS84 lon/lat)"

GeometryTransformFn = Callable[[BaseGeometry], BaseGeometry]


def euclidean_transform_fn(tform: EuclideanTransform) -> GeometryTransformFn:
    def _apply(geom: BaseGeometry) -> BaseGeometry:
        def _fn(x, y, z=None):
            pts = np.column_stack([np.asarray(x, dtype=float), np.asarray(y, dtype=float)])
            out = tform(pts)
            return out[:, 0], out[:, 1]

        return shapely_transform(_fn, geom)

    return _apply


def reproject_fn(source_crs: str, target_crs: str = WGS84_CRS) -> GeometryTransformFn:
    transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)

    def _apply(geom: BaseGeometry) -> BaseGeometry:
        return shapely_transform(lambda x, y, z=None: transformer.transform(x, y), geom)

    return _apply


def transform_feature_collection(fc: FeatureCollection, geom_fn: GeometryTransformFn) -> FeatureCollection:
    return fc.model_copy(
        update={
            "features": [f.model_copy(update={"geometry": geom_fn(f.geometry)}) for f in fc.features]
        }
    )


def transform_planting_points(points: list[PlantingPoint], geom_fn: GeometryTransformFn) -> list[PlantingPoint]:
    return [p.model_copy(update={"geometry": geom_fn(p.geometry)}) for p in points]


def transform_zoning_result(zoning: ZoningResult, geom_fn: GeometryTransformFn) -> ZoningResult:
    return zoning.model_copy(
        update={
            "lawn_raw": geom_fn(zoning.lawn_raw),
            "site_boundary": geom_fn(zoning.site_boundary) if zoning.site_boundary is not None else None,
            "base_area": geom_fn(zoning.base_area),
            "allowed": {plant_type: geom_fn(geom) for plant_type, geom in zoning.allowed.items()},
            "prohibited": [
                zone.model_copy(update={"geometry": geom_fn(zone.geometry)}) for zone in zoning.prohibited
            ],
        }
    )
